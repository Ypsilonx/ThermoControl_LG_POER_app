# -*- coding: utf-8 -*-
"""
FastAPI webová aplikace ThermoControl-LG-POER_app.

Hlavní vstupní bod webového serveru:
- Inicializuje sdílené prostředky (ThinQAPI, MQTT) přes lifespan.
- Mountuje statické soubory a Jinja2 šablony.
- Registruje routery pro jednotlivé oblasti funkcionality.

Spouštění (přes main.py):
    python src/main.py --mode web
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from automation_rules import get_season_for_datetime, load_automation_rules, resolve_scheduled_mode
from command_arbiter import CommandArbiter, CommandRequest, CommandSource, CommandSuperseded
from device_jobs import LG_STATE_KEY, lg_apply_action_job, lg_command_job, lg_device_key
from poer_api import fetch_poer_status
from server_api import ThinQAPI, list_ac_device_ids
from thermal_controller import (
    ThermalControlPolicy,
    ThermalControlState,
    decide_thermal_control,
    derive_policy_for_target,
)
from web.auth import CloudflareAccessMiddleware
from web.ratelimit import RateLimitMiddleware
from web.settings import get_settings
from history.collector import HistoryCollector, parse_lg_push
from history.store import HistoryStore
from web.routes.devices import router as devices_router
from web.routes.control import router as control_router
from web.routes.ws import router as ws_router, manager as ws_manager
from web.routes.mode import router as mode_router
from web.routes.weather import router as weather_router
from web.routes.schedule import router as schedule_router
from web.routes.energy import router as energy_router
from web.routes.poer import router as poer_router
from web.routes.history import router as history_router

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).parent
_STATE_FILE = BASE_DIR.parent.parent / "data" / "state.json"


def _load_control_mode() -> str:
    """
    Načte naposledy uložený control_mode z data/state.json.

    Returns:
        str: "AUTO" nebo "HAND". Výchozí je "AUTO" pokud soubor neexistuje nebo je chybný.
    """
    try:
        if _STATE_FILE.exists():
            data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            mode = data.get("control_mode", "AUTO")
            return mode if mode in ("AUTO", "HAND") else "AUTO"
    except Exception as exc:
        logger.warning("Nelze načíst state.json: %s", exc)
    return "AUTO"


# ---------------------------------------------------------------------------
# Scheduler – background task
# ---------------------------------------------------------------------------

async def _submit_schedule_action(
    arbiter: CommandArbiter,
    api: ThinQAPI,
    device_ids: list[str],
    action: dict | None,
) -> None:
    """
    Odešle akci plánovače všem klimatizacím souběžně přes arbitra.

    Args:
        arbiter:    Sdílený arbitr příkazů.
        api:        Inicializovaná ThinQAPI instance.
        device_ids: ThinQ ID cílových zařízení.
        action:     ``{mode, temperature, wind_strength}`` pro time_on, ``None`` pro time_off.
    """

    async def _one(device_id: str) -> None:
        """Odešle akci jednomu zařízení a zaloguje výsledek."""
        if action is None:
            job = lg_command_job(api, device_id, "power_off", ())
        else:
            job = lg_apply_action_job(api, device_id, action)
        try:
            outcome = await arbiter.submit(CommandRequest(
                lg_device_key(device_id), LG_STATE_KEY, CommandSource.SCHEDULE, job
            ))
            logger.info(
                "✅ Plánovač: akce dokončena (%s...) %s", device_id[:8], outcome.skip_reason or ""
            )
        except CommandSuperseded as exc:
            logger.info("⏰ Plánovač: akce nahrazena (%s...): %s", device_id[:8], exc)
        except Exception as exc:
            logger.error("❌ Plánovač: chyba akce (%s...): %s", device_id[:8], exc)

    await asyncio.gather(*(_one(device_id) for device_id in device_ids))


def _dispatch_schedule_action(
    app: FastAPI,
    api: ThinQAPI,
    device_ids: list[str],
    action: dict | None,
) -> None:
    """
    Spustí akci plánovače na pozadí, aby smyčka plánovače nečekala na arbitra.

    Arbitr může požadavek držet kvůli limitu frekvence a úloha trvá i desítky
    sekund – čekání by způsobilo přeskočení další minuty plánu. Reference na
    task se drží v ``app.state.background_tasks`` (jinak by ho GC mohl zrušit).

    Args:
        app:        FastAPI instance (arbitr + množina běžících tasků).
        api:        Inicializovaná ThinQAPI instance.
        device_ids: ThinQ ID cílových zařízení.
        action:     Akce pro time_on, ``None`` pro time_off.
    """
    task = asyncio.create_task(
        _submit_schedule_action(app.state.arbiter, api, device_ids, action)
    )
    app.state.background_tasks.add(task)
    task.add_done_callback(app.state.background_tasks.discard)


def _create_history_collector() -> HistoryCollector | None:
    """
    Vytvoří sběrač historie podle nastavení (LG_HISTORY_*).

    Returns:
        HistoryCollector | None: Sběrač, nebo None pokud je vypnutý či nejde otevřít DB.
    """
    settings = get_settings()
    if not settings.history_enabled:
        return None
    try:
        store = HistoryStore(BASE_DIR.parent.parent / "data" / "history.db")
    except Exception as exc:
        logger.error("❌ Historie: databázi nelze otevřít, sběr vypnut: %s", exc)
        return None
    return HistoryCollector(
        store,
        os.getenv("LG_POER_API_KEY", "").strip(),
        poll_seconds=settings.history_poll_s,
        retention_days=settings.history_retention_days,
    )


AUTOMATION_TICK_SECONDS = 60


def _job_for_decision(api: ThinQAPI, device_id: str, decision, policy: ThermalControlPolicy):
    """
    Sestaví úlohu pro rozhodnutí PID regulace.

    Args:
        api:       Inicializovaná ThinQAPI instance.
        device_id: ThinQ ID cílové klimatizace.
        decision:  ``ThermalControlDecision`` z ``decide_thermal_control``.
        policy:    Efektivní politika (výchozí cílová teplota).

    Returns:
        JobFactory | None: Úloha, nebo None pokud rozhodnutí nic neodesílá.
    """
    if decision.action == "power_off":
        return lg_command_job(api, device_id, "power_off", ())
    if decision.action == "run" and decision.mode:
        target_temp = decision.target_temperature_c
        if target_temp is None:
            target_temp = policy.target_temperature_c
        return lg_apply_action_job(api, device_id, {
            "mode": decision.mode,
            "temperature": round(float(target_temp), 1),
            "wind_strength": decision.wind_strength,
        })
    return None


async def _run_thermal_regulation_for_device(
    app: FastAPI,
    api: ThinQAPI,
    device_id: str,
    rules,
    weather_cfg,
    base_policy: ThermalControlPolicy,
    indoor_source_cfg: str,
    poer_temp_c: float | None,
    outdoor_online_c: float | None,
    now_local: datetime,
) -> None:
    """
    Vyhodnotí a případně provede PID-like regulaci pro jedno zařízení.

    Příkazy posílá přes arbitra (``app.state.arbiter``) stejnými úlohami
    jako HAND scheduler, takže respektuje preconditions (power_on před
    change_mode), retry v ``ThinQAPI`` a nekoliduje s ručními příkazy.

    Args:
        app:               FastAPI instance (přístup k ``app.state`` pro
                            perzistenci watchdog stavu a deduplikaci příkazů).
        api:                Inicializovaná ThinQAPI instance.
        device_id:          ThinQ ID cílové klimatizace.
        rules:              Validovaná ``AutomationRulesConfig``.
        weather_cfg:        ``rules.weather`` (zkratka pro čitelnost volání).
        base_policy:        Výchozí ``ThermalControlPolicy`` (bez posunu dle cíle).
        indoor_source_cfg:  Nakonfigurovaný zdroj indoor teploty.
        poer_temp_c:        Poslední cachovaná indoor teplota z POER (nebo None).
        outdoor_online_c:   Aktuální venkovní teplota z weather cache (nebo None).
        now_local:          Lokální čas vyhodnocení (pro watchdog a cooldown).
    """
    try:
        status = await api.get_device_status(device_id)
    except Exception as exc:
        logger.warning("⚠️ Automation: nelze načíst stav %s...: %s", device_id[:8], exc)
        return

    if indoor_source_cfg == "poer_api":
        indoor_raw_c = poer_temp_c
        indoor_source = "external_thermostat" if indoor_raw_c is not None else "unknown"
    elif weather_cfg.indoor_current_temperature_c is not None:
        indoor_raw_c = float(weather_cfg.indoor_current_temperature_c)
        indoor_source = "external_thermostat"
    else:
        temp_node = status.get("temperature", {}) if isinstance(status, dict) else {}
        raw_value = temp_node.get("currentTemperature") if isinstance(temp_node, dict) else None
        try:
            indoor_raw_c = float(raw_value) if raw_value is not None else None
        except (TypeError, ValueError):
            indoor_raw_c = None
        indoor_source = "ac_builtin_sensor"

    if indoor_raw_c is None:
        return

    indoor_corrected_c = (
        indoor_raw_c if indoor_source == "external_thermostat"
        else indoor_raw_c + float(weather_cfg.ac_indoor_temperature_proxy_offset_c)
    )

    power_mode = str(status.get("operation", {}).get("airConOperationMode", "POWER_OFF")).upper()
    current_mode = str(status.get("airConJobMode", {}).get("currentJobMode", "")).upper()
    power_on = power_mode == "POWER_ON"

    effective_policy = base_policy
    target_node = status.get("temperature", {}) if isinstance(status, dict) else {}
    current_target_raw = target_node.get("targetTemperature") if isinstance(target_node, dict) else None
    try:
        current_target_c = float(current_target_raw) if current_target_raw is not None else None
    except (TypeError, ValueError):
        current_target_c = None
    if current_target_c is not None:
        effective_policy = derive_policy_for_target(policy=base_policy, target_temperature_c=current_target_c)

    thermal_states: dict[str, ThermalControlState] = app.state.thermal_states
    state = thermal_states.get(device_id) or ThermalControlState()

    decision, next_state = decide_thermal_control(
        indoor_corrected_c=indoor_corrected_c,
        outdoor_online_c=outdoor_online_c,
        current_mode=current_mode,
        power_on=power_on,
        now_local=now_local,
        state=state,
        policy=effective_policy,
    )
    thermal_states[device_id] = next_state

    if decision.action == "keep":
        return

    # Sezónní filtr – PID nesmí obcházet sezónní pravidla (COOL jen v létě,
    # HEAT ne v chladicí sezóně).
    if decision.action == "run" and decision.mode:
        mode_upper = decision.mode.upper()
        if mode_upper == "COOL":
            season_resolution = resolve_scheduled_mode(
                requested_mode=mode_upper, current_time=now_local, rules=rules
            )
            if not season_resolution.allowed or season_resolution.adjusted:
                logger.info(
                    "🤖 PID: COOL blokován/upraven sezónními pravidly (%s), přeskakuji.",
                    season_resolution.reason,
                )
                return
        elif mode_upper == "HEAT":
            current_season = get_season_for_datetime(now_local, rules)
            if current_season in rules.cooling_allowed_seasons:
                logger.info(
                    "🤖 PID: HEAT blokován v sezóně %s (chladicí sezóna), přeskakuji.",
                    current_season,
                )
                return

    signature = (
        decision.action,
        (decision.mode or "").upper(),
        int(round(decision.target_temperature_c)) if decision.target_temperature_c is not None else None,
        (decision.wind_strength or "").upper(),
    )
    last_sig = app.state.thermal_last_signature.get(device_id)
    last_at = app.state.thermal_last_action_at.get(device_id)
    cooldown = timedelta(minutes=max(1, effective_policy.command_cooldown_minutes))
    if last_sig == signature and last_at is not None and (now_local - last_at) < cooldown:
        return

    job = _job_for_decision(api, device_id, decision, effective_policy)
    if job is None:
        return

    try:
        await app.state.arbiter.submit(CommandRequest(
            lg_device_key(device_id), LG_STATE_KEY, CommandSource.AUTOMATION, job
        ))
    except CommandSuperseded as exc:
        logger.info("🤖 Automation: příkaz nahrazen (%s...): %s", device_id[:8], exc)
        return
    except Exception as exc:
        # Podpis se uloží i po selhání (stejně jako dřív) – cooldown pak brání
        # opakování každou minutu při výpadku LG cloudu a šetří limit volání API.
        logger.error("❌ Automation: provedení PID rozhodnutí selhalo pro %s...: %s", device_id[:8], exc)
        app.state.thermal_last_signature[device_id] = signature
        app.state.thermal_last_action_at[device_id] = now_local
        return

    app.state.thermal_last_signature[device_id] = signature
    app.state.thermal_last_action_at[device_id] = now_local
    logger.info("🤖 PID (%s...): %s", device_id[:8], decision.reason)

    await ws_manager.broadcast(
        {
            "type": "automation_decision",
            "device_id": device_id,
            "data": {"action": decision.action, "mode": decision.mode, "reason": decision.reason},
            "timestamp": now_local.astimezone(timezone.utc).isoformat(),
        }
    )


async def _automation_loop(app: FastAPI) -> None:
    """
    Background smyčka AUTO režimu – sezónní pravidla + PID-like termoregulace.

    Bez tohoto tasku přepínač AUTO/HAND ve webu jen ukládal stav
    ``control_mode``, ale žádná logika reálně neřídila zařízení. Tento task
    tuto mezeru zavírá – používá moduly ``automation_rules`` a
    ``thermal_controller`` a stejnou command pipeline jako HAND scheduler.

    Běží pouze pokud je ``control_mode == "AUTO"``. Indoor teplota z POER
    cloudu se cachuje na interval ``weather.refresh_interval_hours``, aby se
    cloud API nezatěžovalo každou minutu.

    Args:
        app: FastAPI aplikační instance (přístup k ``app.state.api``).
    """
    rules_path = BASE_DIR.parent.parent / "data" / "automation_rules.json"
    base_policy = ThermalControlPolicy()
    poer_cache: dict[str, Any] = {"temp_c": None, "fetched_at": None}

    logger.info("🤖 Automation (AUTO) loop spuštěn")

    while True:
        await asyncio.sleep(AUTOMATION_TICK_SECONDS)

        try:
            if getattr(app.state, "control_mode", "AUTO") != "AUTO":
                continue

            api: ThinQAPI | None = getattr(app.state, "api", None)
            if api is None:
                continue

            rules, warning = load_automation_rules(rules_path)
            if warning:
                logger.warning("⚠️ Automation rules: %s", warning)

            weather_cfg = rules.weather
            if not weather_cfg.enabled:
                continue

            now_local = datetime.now()

            indoor_source_cfg = weather_cfg.indoor_current_temperature_source
            if indoor_source_cfg == "poer_api":
                poer_api_key = os.getenv("LG_POER_API_KEY", "").strip()
                cache_age_h = None
                if poer_cache["fetched_at"] is not None:
                    cache_age_h = (now_local - poer_cache["fetched_at"]).total_seconds() / 3600
                refresh_due = cache_age_h is None or cache_age_h >= max(1, weather_cfg.refresh_interval_hours)
                if poer_api_key and refresh_due:
                    poer_status = await fetch_poer_status(
                        api_key=poer_api_key,
                        preferred_device_id=weather_cfg.poer_device_id,
                    )
                    poer_cache["temp_c"] = poer_status.get("current_temperature_c")
                    poer_cache["fetched_at"] = now_local
                    if poer_status.get("error_text"):
                        logger.warning(
                            "⚠️ Automation: POER indoor teplota nedostupná: %s",
                            poer_status["error_text"],
                        )

            outdoor_online_c = None
            cache = getattr(app.state, "weather_cache", None)
            if isinstance(cache, dict):
                outdoor_online_c = cache.get("current_temperature_c")

            device_ids = list(getattr(app.state, "known_device_ids", set()))
            if not device_ids:
                try:
                    device_ids = list_ac_device_ids()
                except Exception:
                    device_ids = []

            for device_id in device_ids:
                await _run_thermal_regulation_for_device(
                    app,
                    api,
                    device_id,
                    rules,
                    weather_cfg,
                    base_policy,
                    indoor_source_cfg,
                    poer_cache["temp_c"],
                    outdoor_online_c,
                    now_local,
                )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("❌ Automation loop: neočekávaná chyba v hlavní smyčce: %s", exc)


MQTT_WATCHDOG_INTERVAL_SECONDS = 120


async def _mqtt_watchdog_loop(app: FastAPI, on_message) -> None:
    """
    Watchdog udržující MQTT real-time push dostupné i po výpadku LG cloudu.

    AWS CRT SDK se sám pokouší o reconnect, ale jen pokud bylo spojení
    jednou úspěšně navázáno a pak *přerušeno*. Pokud selže úvodní
    ``connect_mqtt()`` při startu serveru (např. LG cloud dočasně
    nedostupný), spojení zůstane trvale odpojené bez další snahy – appka by
    pak fungovala jen na 5minutovém HTTP pollingu z prohlížeče až do ručního
    restartu. Tento watchdog kontroluje stav každých
    ``MQTT_WATCHDOG_INTERVAL_SECONDS`` a v případě odpojení se pokusí znovu
    připojit a obnovit event subscripce zařízení.

    Args:
        app:        FastAPI aplikační instance.
        on_message: MQTT→WS bridge callback (stejný jako při startu).
    """
    logger.info("🔧 MQTT watchdog spuštěn")
    while True:
        await asyncio.sleep(MQTT_WATCHDOG_INTERVAL_SECONDS)
        try:
            api: ThinQAPI | None = getattr(app.state, "api", None)
            if api is None or api.mqtt_connected:
                continue

            logger.warning("⚠️ MQTT watchdog: spojení není aktivní, pokouším se znovu připojit...")
            mqtt_ok = await api.connect_mqtt(on_message=on_message)
            app.state.mqtt_connected = mqtt_ok
            if mqtt_ok:
                logger.info("✅ MQTT watchdog: spojení obnoveno")
                for dev_id in getattr(app.state, "known_device_ids", set()):
                    await api.subscribe_device_events(dev_id)
            else:
                logger.warning(
                    "⚠️ MQTT watchdog: pokus o obnovení selhal, zkusím znovu za %ds",
                    MQTT_WATCHDOG_INTERVAL_SECONDS,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("❌ MQTT watchdog: neočekávaná chyba: %s", exc)


def _scheduler_enabled(control_mode: str, settings: dict) -> bool:
    """
    Rozhodne, zda má plánovač v této minutě spouštět akce.

    Plánovač patří k režimu HAND. V AUTO řídí klimatizaci automatika a obě by
    posílaly příkazy se stejným klíčem a prioritou – navzájem by se přepisovaly.

    Args:
        control_mode: ``"AUTO"`` nebo ``"HAND"``
        settings:     Sekce ``settings`` ze schedule.json

    Returns:
        bool: True pokud je režim HAND a zapnuté ``enable_scheduler`` i ``auto_execute``
    """
    return (
        control_mode == "HAND"
        and bool(settings.get("enable_scheduler"))
        and bool(settings.get("auto_execute"))
    )


async def _scheduler_loop(app: FastAPI) -> None:
    """
    Pozadí smyčka plánovače – každou minutu kontroluje schedule.json.

    Čeká vždy na začátek příští minuty, pak projde aktivní záznamy
    a spustí time_on / time_off akce, jejichž čas odpovídá aktuálnímu
    HH:MM a den v týdnu je v povoleném seznamu (nebo je seznam prázdný).

    Akce se provedou jen v režimu HAND a pokud je v settings.json zapnuto
    ``enable_scheduler`` i ``auto_execute``. Každá akce se v danou minutu
    provede nejvýše jednou (deduplication přes ``_executed`` set).

    Args:
        app: FastAPI aplikační instance (přístup k app.state.api).
    """
    _executed: set[str] = set()  # "entry_id:YYYY-MM-DD HH:MM:on/off"
    schedule_path = BASE_DIR.parent.parent / "data" / "schedule.json"
    logger.info("⏰ Plánovač spuštěn")

    while True:
        # Počkat na začátek příští minuty (+0.1 s tolerance)
        now = datetime.now()
        await asyncio.sleep(60 - now.second + 0.1)

        now = datetime.now()
        current_hhmm = now.strftime("%H:%M")
        weekday = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][now.weekday()]
        today = now.strftime("%Y-%m-%d")

        try:
            if not schedule_path.exists():
                continue

            sched_data = json.loads(schedule_path.read_text(encoding="utf-8"))
            settings = sched_data.get("settings", {})

            control_mode = getattr(app.state, "control_mode", "AUTO")
            if not _scheduler_enabled(control_mode, settings):
                continue

            api: ThinQAPI | None = getattr(app.state, "api", None)
            if api is None:
                continue

            device_ids = list(getattr(app.state, "known_device_ids", set()))
            if not device_ids:
                # Záloha: pokud known_device_ids zůstalo prázdné (selhání API
                # při startu), načteme klimatizace přímo z devices.json.
                try:
                    device_ids = list_ac_device_ids()
                    if device_ids:
                        logger.info(
                            "⏰ Plánovač: known_device_ids prázdné, záloha ze souboru (%d AC)",
                            len(device_ids),
                        )
                except Exception as _exc:
                    logger.warning("⏰ Plánovač: záloha devices.json selhala: %s", _exc)
            if not device_ids:
                continue

            for entry in sched_data.get("schedules", []):
                if not entry.get("enabled"):
                    continue

                days = entry.get("days") or []
                if days and weekday not in days:
                    continue

                entry_id = entry.get("id", "")
                action = entry.get("action") or {}
                name = entry.get("name", entry_id)

                # Plán se vztahuje na všechny sledované klimatizace, ne jen na
                # jednu – dřívější verze bralo `next(iter(device_ids))`, což
                # při více AC ovládalo jen nedeterministicky vybrané zařízení.
                # time_on
                key_on = f"{entry_id}:{today} {current_hhmm}:on"
                if entry.get("time_on") == current_hhmm and key_on not in _executed:
                    _executed.add(key_on)
                    logger.info("⏰ Plánovač: time_on pro '%s' (%s)", name, current_hhmm)
                    _dispatch_schedule_action(app, api, device_ids, action)

                # time_off
                time_off = entry.get("time_off")
                key_off = f"{entry_id}:{today} {current_hhmm}:off"
                if time_off and time_off == current_hhmm and key_off not in _executed:
                    _executed.add(key_off)
                    logger.info("⏰ Plánovač: time_off pro '%s' (%s)", name, current_hhmm)
                    _dispatch_schedule_action(app, api, device_ids, None)

            # Vyčistit záznamy staršího dne
            _executed = {k for k in _executed if f":{today} " in k}

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("❌ Plánovač: chyba v hlavní smyčce: %s", exc)


async def _weather_refresh_loop(app: FastAPI) -> None:
    """
    Pozadí smyčka pro periodickou aktualizaci předpovědi počasí z ČHMÚ.

    Interval čerstvého stažení řídí ``weather.refresh_interval_hours``
    z ``automation_rules.json`` (výchozí 3 hodiny). Výsledek se ukládá do
    ``app.state.weather_cache``. Běží nezávisle na ``control_mode`` –
    data jsou aktuální vždy; automatika i manuální režim z nich čtou.

    Při startu aplikace se první fetch provede okamžitě (bez úvodního
    čekání), aby byla data dostupná ihned po spuštění serveru.

    Args:
        app: FastAPI aplikační instance (přístup k app.state).
    """
    from web.routes.weather import (
        _fetch_weather_data,
        _load_weather_config,
        save_weather_cache,
    )

    logger.info("🌤️ Weather refresh loop spuštěn")
    while True:
        # Výchozí interval pro případ, že je počasí vypnuté nebo nastane chyba
        sleep_seconds = 3 * 3600
        try:
            config = _load_weather_config()
            if config.get("enabled", False):
                interval_h = float(config.get("refresh_interval_hours", 3))
                sleep_seconds = max(1.0, interval_h) * 3600
                result = await _fetch_weather_data(config)
                if "error" not in result:
                    app.state.weather_cache = result
                    app.state.weather_cache_time = datetime.now(timezone.utc)
                    save_weather_cache(result)
                    if getattr(app.state, "history", None) is not None:
                        await app.state.history.record_weather(result)
                    logger.info(
                        "🌤️ Počasí aktualizováno z ČHMÚ meteogram (další za %.0f h)",
                        interval_h,
                    )
                else:
                    logger.warning("⚠️ Počasí: aktualizace selhala – %s", result.get("error"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("❌ Počasí: neočekávaná chyba v refresh loop: %s", exc)

        await asyncio.sleep(sleep_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Životní cyklus aplikace.

    Startup:  Inicializuje sdílenou instanci ThinQAPI (HTTP session).
              Pokud inicializace selže (chybějící config.json, neplatný token),
              aplikace přesto nastartuje – endpointy vyžadující API vrátí HTTP 503.
    Shutdown: Čistě odpojí MQTT a uzavře HTTP session.
    """
    # --- Startup ---
    try:
        api = ThinQAPI()
        await api.initialize()
        app.state.api = api
        app.state.api_error = None
        logger.info("✅ ThinQAPI inicializováno")
        # Pre-cache device IDs klimatizací pro MQTT topic parsing.
        # Aplikace cílí výhradně na klimatizace – ostatní zařízení
        # (lednice, pračka, ...) v devices.json záměrně ignorujeme.
        try:
            app.state.known_device_ids = set(list_ac_device_ids())
            logger.info(
                "ℹ️ Sledováno %d klimatizací", len(app.state.known_device_ids)
            )
        except Exception:
            app.state.known_device_ids = set()
    except Exception as exc:
        logger.error(f"❌ ThinQAPI inicializace selhala: {exc}")
        app.state.api = None
        app.state.api_error = str(exc)
        app.state.known_device_ids = set()

    # Inicializace sdíleného in-memory stavu (mode se načítá z state.json)
    app.state.control_mode = _load_control_mode()

    # Jediná brána pro příkazy zařízením – web, scheduler i automatika.
    app.state.arbiter = CommandArbiter()
    app.state.background_tasks = set()

    # Historie dat pro ladění automatiky (data/history.db).
    app.state.history = _create_history_collector()

    # Perzistentní stav PID regulace (watchdog + deduplikace příkazů) mezi tiky.
    app.state.thermal_states = {}
    app.state.thermal_last_signature = {}
    app.state.thermal_last_action_at = {}

    # Načti poslední uloženou předpověď z disku, aby byla data dostupná
    # ihned po restartu serveru (bez čekání na první background fetch).
    from web.routes.weather import load_weather_cache
    cached, cached_time = load_weather_cache()
    app.state.weather_cache = cached
    app.state.weather_cache_time = cached_time
    if cached is not None:
        logger.info("🌤️ Načtena uložená předpověď z disku (weather_cache.json)")

    # --- MQTT bridge → WebSocket ---
    # Zachytíme aktuální asyncio smyčku, která bude použita pro
    # přechod z C++ vlákna AWS CRT SDK do asyncio (run_coroutine_threadsafe).
    loop = asyncio.get_running_loop()

    def _on_mqtt_message(topic, payload, dup, qos, retain, **kwargs):
        """
        MQTT callback volaný z AWS CRT C++ vlákna.
        Přemostí zprávu do asyncio smyčky a odešle všem WS klientům.
        """
        try:
            if isinstance(payload, (bytes, bytearray)):
                data = json.loads(payload.decode("utf-8"))
            else:
                data = payload if isinstance(payload, dict) else {}

            # ThinQ Connect posílá {"pushType": "DEVICE_STATUS", "deviceId", "report"}
            # na společný topic klienta (ověřeno 2026-09-30) – ID není v topicu.
            known_ids = getattr(app.state, "known_device_ids", set())
            device_id, device_status = parse_lg_push(topic, data, known_ids)
            if device_id is None:
                return

            message = {
                "type": "device_status",
                "device_id": device_id,
                "data": device_status,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            asyncio.run_coroutine_threadsafe(ws_manager.broadcast(message), loop)
            history = getattr(app.state, "history", None)
            if history is not None:
                asyncio.run_coroutine_threadsafe(
                    history.record_lg_status(device_id, device_status), loop
                )
        except Exception as exc:
            logger.error(f"❌ MQTT→WS bridge chyba: {exc}")

    if app.state.api is not None:
        mqtt_ok = await app.state.api.connect_mqtt(on_message=_on_mqtt_message)
        app.state.mqtt_connected = mqtt_ok
        if mqtt_ok:
            logger.info("✅ MQTT připojeno – real-time push aktivní")
            # Přihlásit event subscripci pro každé zařízení.
            # Bez tohoto volání LG platforma neposílá změny stavu zařízení
            # z externích zdrojů (telefon, dálkový ovladač) přes MQTT.
            for _dev_id in app.state.known_device_ids:
                await app.state.api.subscribe_device_events(_dev_id)
        else:
            logger.warning("⚠️ MQTT nepřipojeno – WS push nebude aktivní")
    else:
        app.state.mqtt_connected = False

    # Spustit background tasky
    scheduler_task = asyncio.create_task(_scheduler_loop(app))
    weather_task = asyncio.create_task(_weather_refresh_loop(app))
    automation_task = asyncio.create_task(_automation_loop(app))
    mqtt_watchdog_task = asyncio.create_task(_mqtt_watchdog_loop(app, _on_mqtt_message))
    history_task = None
    if app.state.history is not None:
        # Úvodní stav LG čte až úloha sběru – start serveru na LG nečeká.
        history_task = asyncio.create_task(app.state.history.run(
            lambda: app.state.api, lambda: list(app.state.known_device_ids)
        ))

    yield

    # --- Shutdown ---
    scheduler_task.cancel()
    weather_task.cancel()
    automation_task.cancel()
    mqtt_watchdog_task.cancel()
    if history_task is not None:
        history_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        logger.info("⏰ Plánovač zastaven")
    try:
        await weather_task
    except asyncio.CancelledError:
        logger.info("🌤️ Weather refresh loop zastaven")
    try:
        await automation_task
    except asyncio.CancelledError:
        logger.info("🤖 Automation loop zastaven")
    try:
        await mqtt_watchdog_task
    except asyncio.CancelledError:
        logger.info("🔧 MQTT watchdog zastaven")
    if history_task is not None:
        try:
            await history_task
        except asyncio.CancelledError:
            logger.info("📈 Sběr historie zastaven")
        # Hranice pro intervaly topení LG – neběží přes dobu, kdy server nejede.
        await app.state.history.mark_stopped()
    await app.state.arbiter.close()
    logger.info("🔧 Arbitr příkazů ukončen")

    api_instance: ThinQAPI | None = getattr(app.state, "api", None)
    if api_instance is not None:
        await api_instance.close()
        logger.info("ThinQAPI session uzavřena")


_settings = get_settings()

if not _settings.auth_is_cloudflare:
    logger.warning(
        "⚠️ AUTENTIZACE VYPNUTA (LG_AUTH_MODE=%s). "
        "Nevystavujte server na internet bez Cloudflare Access!",
        _settings.auth_mode,
    )

app = FastAPI(
    title="ThermoControl-LG-POER_app",
    description="Webové rozhraní pro ovládání klimatizace, POER termostatu a plánování",
    version="1.0.0",
    lifespan=lifespan,
    # Swagger UI / ReDoc se v produkci vypne (LG_DOCS_ENABLED=false).
    docs_url="/docs" if _settings.docs_enabled else None,
    redoc_url="/redoc" if _settings.docs_enabled else None,
    openapi_url="/openapi.json" if _settings.docs_enabled else None,
)

# --- Middleware ---
# Pozor na pořadí: poslední přidaný middleware běží jako první (vnější obal).
# Autentizaci chceme jako nejvnější vrstvu – odmítne neoprávněný provoz
# dříve, než se vůbec dostane k rate limiteru či handlerům.
app.add_middleware(RateLimitMiddleware, settings=_settings)
app.add_middleware(CloudflareAccessMiddleware, settings=_settings)

# Statické soubory (CSS, JS, obrázky)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# Jinja2 šablony
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# ---------------------------------------------------------------------------
# Routery
# ---------------------------------------------------------------------------
app.include_router(devices_router)
app.include_router(control_router)
app.include_router(ws_router)
app.include_router(mode_router)
app.include_router(weather_router)
app.include_router(schedule_router)
app.include_router(energy_router)
app.include_router(poer_router)
app.include_router(history_router)


# ---------------------------------------------------------------------------
# Základní endpointy
# ---------------------------------------------------------------------------

@app.get("/health")
async def health_check():
    """
    Health check endpoint.

    Používá se Docker HEALTHCHECK direktivou i externím monitoringem.

    Returns:
        dict: Stav služby ``{"status": "ok"}``.
    """
    return {"status": "ok", "service": "lg-klimatizace"}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """
    Hlavní dashboard – přehled stavu zařízení.

    Args:
        request: HTTP požadavek (předáván do Jinja2 kontextu).

    Returns:
        HTMLResponse: Vyrendrovaný dashboard.html.
    """
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"active_page": "dashboard"},
    )


@app.get("/automation", response_class=HTMLResponse)
async def automation_page(request: Request):
    """
    Stránka automatizace – přepínač HAND/AUTO a předpověď počasí.

    Args:
        request: HTTP požadavek.

    Returns:
        HTMLResponse: Vyrendrovaný automation.html.
    """
    return templates.TemplateResponse(
        request=request,
        name="automation.html",
        context={"active_page": "automation"},
    )


@app.get("/scheduler", response_class=HTMLResponse)
async def scheduler_page(request: Request):
    """
    Stránka plánování – přehled naplánovaných akcí.

    Args:
        request: HTTP požadavek.

    Returns:
        HTMLResponse: Vyrendrovaný scheduler.html.
    """
    return templates.TemplateResponse(
        request=request,
        name="scheduler.html",
        context={"active_page": "scheduler"},
    )
