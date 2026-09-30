# POER pro více termostatů – implementační plán

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Aplikace vidí a ovládá oba POER termostaty (Kuchobyvak `fee89300f2a5`, Koupelna `fee89300fac5`); nastavení teploty z aplikace přepne termostat do ručního režimu, aby ho nepřepsal vlastní program POER; méně volání POER cloudu.

**Architecture:** `poer_api.py` dostane nízkoúrovňové `_post_ha` + parsovací funkce, seznam zařízení z `SYNC` s dlouhou cache a stav všech zařízení jedním `QUERY`. Stávající `fetch_poer_status` zůstane jako obal (volá ho automatika, weather route a legacy GUI). `send_poer_command` s konkrétním ID už nevolá `SYNC`/`QUERY`. Úloha `poer_command_job` pro `set_temp` pošle `heat` + teplotu jako jednu nepřerušenou úlohu arbitra. Routy přijmou volitelné `device_id` (validované proti seznamu zařízení), dashboard dostane přepínač termostatu.

**Tech Stack:** Python 3.12, aiohttp, FastAPI, Alpine.js (dashboard), stdlib `unittest` přes pytest.

**Spec:** `docs/superpowers/specs/2026-09-30-zonove-rizeni-topeni-design.md` (kapitola 4.1, podprojekt 3)

## Global Constraints

- Kód, komentáře a docstringy česky; docstring u každé funkce/třídy.
- Importy uvnitř projektu bez prefixu; flake8 `max-line-length = 100`, `max-complexity = 10`; lint přes `uvx flake8`.
- Žádné nové závislosti.
- Zjištěno živým testem (2026-09-30): `auto` = program termostatu, `heat` = ruční režim s vlastní uloženou ruční teplotou (přepnutí do `heat` ji okamžitě aktivuje), `eco` = předvolba away. Více příkazů v jednom `EXECUTE` POER nezpracuje – provede jen první. Proto `heat` a teplota jako **dva po sobě jdoucí** požadavky v jedné úloze arbitra.
- Bez `device_id` se chování nemění: použije se `weather.poer_device_id` z konfigurace, jinak první termostat na účtu.
- Legacy Tkinter GUI se nemění (dál používá `fetch_poer_status` / `send_poer_command` se stejnou signaturou).

## Review Focus

- Neznámé `device_id` z prohlížeče → HTTP 404, žádný příkaz do cloudu. Test: `test_unknown_device_returns_404` (Task 3).
- Termostat offline (`online: false`) → stav s `error_text`, dashboard ukáže chybu, žádná výjimka. Test: `test_offline_device_has_error_text` (Task 1).
- Cloud vrátí u `EXECUTE` HTTP 200, ale `status: "ERROR"` → příkaz se hlásí jako neúspěšný (arbitr zopakuje). Test: `test_execute_error_status_is_failure` (Task 1).
- Po úspěšném příkazu se zneplatní i cache stavu všech termostatů, jinak dashboard 20 s ukazuje starou hodnotu. Test: `test_command_clears_all_status_caches` (Task 1).
- Automatika (check_noop) nad termostatem v `auto` se stejnou teplotou nesmí příkaz přeskočit – musí ho přepnout do `heat`. Test: `test_noop_check_in_auto_mode_switches_to_heat` (Task 2).

---

## File Structure

| Soubor | Akce | Odpovědnost |
|---|---|---|
| `src/poer_api.py` | Modify | `_post_ha`, parsování SYNC/QUERY, `fetch_poer_devices`, `fetch_poer_statuses(_cached)`, přepis `fetch_poer_status`, `send_poer_command` bez zbytečných volání |
| `tests/test_poer_api.py` | Create | testy klienta na zaznamenaných odpovědích |
| `src/device_jobs.py` | Modify | `set_temp` = `heat` + teplota |
| `tests/test_device_jobs.py` | Modify | úprava POER testů + nové |
| `src/web/routes/poer.py` | Modify | `GET /devices`, `device_id` ve status/příkazech, validace |
| `tests/test_web_command_routes.py` | Modify | testy výběru zařízení |
| `src/web/templates/dashboard.html` | Modify | přepínač termostatu, `device_id` v požadavcích, oprava min/max |
| `CLAUDE.md` | Modify | zmínka o více termostatech a ručním režimu |

---

### Task 1: POER klient pro více termostatů

**Files:**
- Modify: `src/poer_api.py` (nahradit `fetch_poer_indoor_temperature` + `fetch_poer_status`, ř. ~58–256; upravit `fetch_poer_status_cached` a `send_poer_command`)
- Test: `tests/test_poer_api.py`

**Interfaces:**
- Produces:
  - `class PoerApiError(Exception)`
  - `async fetch_poer_devices(api_key: str, session=None, ttl_seconds: float = 3600.0) -> list[dict]` – položka `{"device_id", "name", "min_temp_c", "max_temp_c"}`; vyhazuje `PoerApiError`
  - `async fetch_poer_statuses(api_key: str, session=None) -> list[dict]` – stav každého termostatu; vyhazuje `PoerApiError`
  - `async fetch_poer_statuses_cached(api_key: str, session=None, ttl_seconds: float = 20.0) -> list[dict]`
  - Stavový dict (i z `fetch_poer_status`): `current_temperature_c, current_humidity_pct, target_temperature_c, device_id, name, online, mode, preset, action, min_temp_c, max_temp_c, error_text`
  - `fetch_poer_status(api_key, preferred_device_id=None, session=None) -> dict` – beze změny signatury
  - `send_poer_command(api_key, endpoint, data, preferred_device_id=None, session=None) -> dict` – beze změny signatury; s `preferred_device_id` jen 1 HTTP volání

- [ ] **Step 1: Napiš padající testy**

Vytvoř `tests/test_poer_api.py`:

```python
"""Testy POER klienta pro více termostatů na zaznamenaných odpovědích cloudu."""

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import poer_api  # noqa: E402

KEY = "eutoken"

# Zaznamenáno 2026-09-30 (zkráceno na použitá pole).
SYNC = {"payload": {"devices": [
    {"id": "fee89300f2a5", "name": {"name": "Kuchobyvak"},
     "attributes": {"thermostatTemperatureRange": {"maxThresholdCelsius": 32,
                                                   "minThresholdCelsius": 5}}},
    {"id": "fee89300fac5", "name": {"name": "Koupelna"},
     "attributes": {"thermostatTemperatureRange": {"maxThresholdCelsius": 32,
                                                   "minThresholdCelsius": 5}}},
]}}
QUERY = {"payload": {"devices": {
    "fee89300f2a5": {"online": True, "status": "SUCCESS", "thermostatAction": "idle",
                     "thermostatHumidityAmbient": 49, "thermostatMode": "auto",
                     "thermostatTemperatureAmbient": 21.8, "thermostatTemperatureSetpoint": 20},
    "fee89300fac5": {"online": True, "status": "SUCCESS", "thermostatAction": "heating",
                     "thermostatHumidityAmbient": 54, "thermostatMode": "heat",
                     "thermostatTemperatureAmbient": 20.6, "thermostatTemperatureSetpoint": 22},
}}}
EXECUTE_OK = {"payload": {"commands": [{"ids": ["fee89300fac5"], "status": "SUCCESS"}]}}
EXECUTE_ERR = {"payload": {"commands": [{"ids": ["fee89300fac5"], "status": "ERROR"}]}}


class PoerClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        poer_api._devices_cache.clear()
        poer_api._status_cache.clear()

    async def test_statuses_for_all_devices(self) -> None:
        with patch.object(poer_api, "_post_ha", AsyncMock(side_effect=[SYNC, QUERY])):
            statuses = await poer_api.fetch_poer_statuses(KEY)
        self.assertEqual([s["device_id"] for s in statuses], ["fee89300f2a5", "fee89300fac5"])
        bathroom = statuses[1]
        self.assertEqual(bathroom["name"], "Koupelna")
        self.assertEqual(bathroom["current_temperature_c"], 20.6)
        self.assertEqual(bathroom["target_temperature_c"], 22.0)
        self.assertEqual(bathroom["mode"], "heat")
        self.assertTrue(bathroom["online"])
        self.assertIsNone(bathroom["error_text"])
        self.assertEqual(bathroom["min_temp_c"], 5.0)

    async def test_device_list_is_cached(self) -> None:
        post = AsyncMock(side_effect=[SYNC, QUERY, QUERY])
        with patch.object(poer_api, "_post_ha", post):
            await poer_api.fetch_poer_statuses(KEY)
            await poer_api.fetch_poer_statuses(KEY)
        # SYNC jen jednou, QUERY dvakrát
        self.assertEqual(post.await_count, 3)

    async def test_status_picks_preferred_device(self) -> None:
        with patch.object(poer_api, "_post_ha", AsyncMock(side_effect=[SYNC, QUERY])):
            status = await poer_api.fetch_poer_status(KEY, "fee89300fac5")
        self.assertEqual(status["device_id"], "fee89300fac5")

    async def test_status_falls_back_to_first_device(self) -> None:
        with patch.object(poer_api, "_post_ha", AsyncMock(side_effect=[SYNC, QUERY])):
            status = await poer_api.fetch_poer_status(KEY, "neznamy")
        self.assertEqual(status["device_id"], "fee89300f2a5")

    async def test_status_error_is_returned_as_dict(self) -> None:
        err = poer_api.PoerApiError("POER SYNC selhal: 500")
        with patch.object(poer_api, "_post_ha", AsyncMock(side_effect=err)):
            status = await poer_api.fetch_poer_status(KEY)
        self.assertIsNone(status["device_id"])
        self.assertEqual(status["error_text"], "POER SYNC selhal: 500")

    async def test_offline_device_has_error_text(self) -> None:
        query = {"payload": {"devices": {"fee89300f2a5": {"online": False},
                                         "fee89300fac5": {"online": False}}}}
        with patch.object(poer_api, "_post_ha", AsyncMock(side_effect=[SYNC, query])):
            status = await poer_api.fetch_poer_status(KEY, "fee89300fac5")
        self.assertFalse(status["online"])
        self.assertEqual(status["error_text"], "POER termostat Koupelna je offline.")

    async def test_invalid_key_is_error(self) -> None:
        status = await poer_api.fetch_poer_status("xx")
        self.assertIn("Neplatny POER API key", status["error_text"])

    async def test_command_with_device_id_makes_single_call(self) -> None:
        post = AsyncMock(return_value=EXECUTE_OK)
        with patch.object(poer_api, "_post_ha", post):
            result = await poer_api.send_poer_command(
                KEY, "set_temp", {"temperature": 21.0}, "fee89300fac5"
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["device_id"], "fee89300fac5")
        post.assert_awaited_once()
        payload = post.await_args.args[1]
        self.assertEqual(payload["inputs"][0]["intent"], "action.devices.EXECUTE")
        command = payload["inputs"][0]["payload"]["commands"][0]
        self.assertEqual(command["devices"], [{"id": "fee89300fac5"}])
        self.assertEqual(command["execution"][0]["params"],
                         {"thermostatTemperatureSetpoint": 21.0})

    async def test_command_without_device_id_resolves_first(self) -> None:
        post = AsyncMock(side_effect=[SYNC, QUERY, EXECUTE_OK])
        with patch.object(poer_api, "_post_ha", post):
            result = await poer_api.send_poer_command(KEY, "set_mode", {"mode": "heat"})
        self.assertTrue(result["success"])
        self.assertEqual(result["device_id"], "fee89300f2a5")

    async def test_execute_error_status_is_failure(self) -> None:
        with patch.object(poer_api, "_post_ha", AsyncMock(return_value=EXECUTE_ERR)):
            result = await poer_api.send_poer_command(
                KEY, "set_temp", {"temperature": 21.0}, "fee89300fac5"
            )
        self.assertFalse(result["success"])
        self.assertIn("ERROR", result["error_text"])

    async def test_command_clears_all_status_caches(self) -> None:
        poer_api._status_cache[f"{KEY}:*"] = (0.0, [])
        poer_api._status_cache[f"{KEY}:fee89300f2a5"] = (0.0, {})
        with patch.object(poer_api, "_post_ha", AsyncMock(return_value=EXECUTE_OK)):
            await poer_api.send_poer_command(KEY, "set_temp", {"temperature": 21.0}, "fee89300fac5")
        self.assertEqual(poer_api._status_cache, {})

    async def test_statuses_cached(self) -> None:
        post = AsyncMock(side_effect=[SYNC, QUERY])
        with patch.object(poer_api, "_post_ha", post):
            first = await poer_api.fetch_poer_statuses_cached(KEY)
            second = await poer_api.fetch_poer_statuses_cached(KEY)
        self.assertEqual(first, second)
        self.assertEqual(post.await_count, 2)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Spusť testy a ověř, že padají**

Run: `uv run pytest tests/test_poer_api.py -q`
Expected: FAIL – `AttributeError: ... '_devices_cache'` / `'_post_ha'`.

- [ ] **Step 3: Implementuj klienta**

V `src/poer_api.py`:

a) Za `_status_cache_lock = asyncio.Lock()` doplň a změň typ cache:

```python
_status_cache: dict[str, tuple[float, Any]] = {}
```

(nahrazuje dosavadní `_status_cache: dict[str, tuple[float, dict[str, Any]]] = {}`), a přidej:

```python
# Seznam termostatů se prakticky nemění – SYNC stačí jednou za hodinu.
_DEVICES_CACHE_TTL_S = 3600.0
_devices_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


class PoerApiError(Exception):
    """Chyba komunikace s POER cloudem (neplatný klíč, síť, HTTP status)."""
```

b) Smaž funkce `fetch_poer_indoor_temperature` a `fetch_poer_status` (celé, až po řádek před `async def fetch_poer_status_cached`) a vlož místo nich:

```python
def _to_float(value: Any) -> float | None:
    """Převede hodnotu na float, jinak vrátí None."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _empty_status(error_text: str, device_id: str | None = None) -> dict[str, Any]:
    """Vrátí stav termostatu bez dat s chybovou zprávou."""
    return {
        "current_temperature_c": None,
        "current_humidity_pct": None,
        "target_temperature_c": None,
        "device_id": device_id,
        "name": None,
        "online": False,
        "mode": None,
        "preset": None,
        "action": None,
        "min_temp_c": None,
        "max_temp_c": None,
        "error_text": error_text,
    }


async def _post_ha(
    api_key: str,
    payload: dict[str, Any],
    session: aiohttp.ClientSession | None = None,
) -> Any:
    """
    Odešle požadavek na POER Home-Assistant endpoint a vrátí JSON odpověď.

    Args:
        api_key: POER API klíč (prefix cn/eu + token)
        payload: Tělo požadavku (SYNC / QUERY / EXECUTE)
        session: Volitelná sdílená aiohttp session

    Returns:
        Any: Dekódovaná JSON odpověď

    Raises:
        PoerApiError: Neplatný klíč, síťová chyba nebo HTTP status různý od 200
    """
    resolved = _resolve_poer_endpoint_and_token(api_key)
    if resolved is None:
        raise PoerApiError("Neplatny POER API key (chybi prefix cn/eu nebo token).")
    api_url, token = resolved
    url = f"{api_url.rstrip('/')}/speaker/ha/v1.0"
    intent = payload["inputs"][0]["intent"].rsplit(".", 1)[-1]

    own_session = session is None
    client = session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))
    try:
        async with client.post(url, json=payload, headers=_build_poer_headers(token)) as response:
            if response.status != 200:
                text = await response.text()
                raise PoerApiError(f"POER {intent} selhal: {response.status} {text}")
            return await response.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise PoerApiError(f"POER sitova chyba: {exc}") from exc
    finally:
        if own_session:
            await client.close()


def _parse_sync_devices(sync_data: Any) -> list[dict[str, Any]]:
    """
    Vytáhne termostaty z odpovědi SYNC.

    Args:
        sync_data: JSON odpověď SYNC

    Returns:
        list[dict]: ``{"device_id", "name", "min_temp_c", "max_temp_c"}`` pro každý termostat
    """
    payload = sync_data.get("payload", {}) if isinstance(sync_data, dict) else {}
    devices = payload.get("devices", []) if isinstance(payload, dict) else []
    result = []
    for item in devices if isinstance(devices, list) else []:
        device_id = str(item.get("id") or "").strip()
        if not device_id:
            continue
        name_info = item.get("name")
        name = name_info.get("name") if isinstance(name_info, dict) else None
        attributes = item.get("attributes") or {}
        temp_range = attributes.get("thermostatTemperatureRange") or {}
        result.append({
            "device_id": device_id,
            "name": name or device_id,
            "min_temp_c": _to_float(temp_range.get("minThresholdCelsius")),
            "max_temp_c": _to_float(temp_range.get("maxThresholdCelsius")),
        })
    return result


def _parse_device_status(device: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """
    Sestaví stav jednoho termostatu ze záznamu SYNC a stavu z QUERY.

    Režim ``eco`` se hlásí jako (``heat``, ``away``) – stejně jako dřív.

    Args:
        device: Položka z ``_parse_sync_devices``
        state:  Stav zařízení z QUERY (``payload.devices[<id>]``)

    Returns:
        dict: Stav termostatu (viz ``_empty_status``)
    """
    online = bool(state.get("online", True))
    current_temperature_c = _to_float(state.get("thermostatTemperatureAmbient"))
    mode = str(state.get("thermostatMode") or "").strip() or None
    preset = "home"
    if mode == "eco":
        mode, preset = "heat", "away"

    error_text = None
    if not online:
        error_text = f"POER termostat {device['name']} je offline."
    elif current_temperature_c is None:
        error_text = "POER nevratil thermostatTemperatureAmbient."

    return {
        "current_temperature_c": current_temperature_c,
        "current_humidity_pct": _to_float(state.get("thermostatHumidityAmbient")),
        "target_temperature_c": _to_float(state.get("thermostatTemperatureSetpoint")),
        "device_id": device["device_id"],
        "name": device["name"],
        "online": online,
        "mode": mode,
        "preset": preset,
        "action": str(state.get("thermostatAction") or "").strip() or None,
        "min_temp_c": device["min_temp_c"],
        "max_temp_c": device["max_temp_c"],
        "error_text": error_text,
    }


async def fetch_poer_devices(
    api_key: str,
    session: aiohttp.ClientSession | None = None,
    ttl_seconds: float = _DEVICES_CACHE_TTL_S,
) -> list[dict[str, Any]]:
    """
    Vrátí seznam termostatů na účtu (SYNC, dlouhodobě cachováno).

    Args:
        api_key:     POER API klíč
        session:     Volitelná sdílená aiohttp session
        ttl_seconds: Platnost cache v sekundách

    Returns:
        list[dict]: ``{"device_id", "name", "min_temp_c", "max_temp_c"}``

    Raises:
        PoerApiError: Chyba komunikace nebo účet bez termostatů
    """
    cached = _devices_cache.get(api_key)
    if cached is not None and (time.monotonic() - cached[0]) < ttl_seconds:
        return cached[1]
    sync_data = await _post_ha(
        api_key, {"requestId": "111", "inputs": [{"intent": "action.devices.SYNC"}]}, session
    )
    devices = _parse_sync_devices(sync_data)
    if not devices:
        raise PoerApiError("POER nevratil zadna zarizeni.")
    _devices_cache[api_key] = (time.monotonic(), devices)
    return devices


async def fetch_poer_statuses(
    api_key: str,
    session: aiohttp.ClientSession | None = None,
) -> list[dict[str, Any]]:
    """
    Načte stav všech termostatů jedním QUERY.

    Args:
        api_key: POER API klíč
        session: Volitelná sdílená aiohttp session

    Returns:
        list[dict]: Stav každého termostatu v pořadí ze SYNC

    Raises:
        PoerApiError: Chyba komunikace
    """
    devices = await fetch_poer_devices(api_key, session)
    query = {
        "requestId": "112",
        "inputs": [{
            "intent": "action.devices.QUERY",
            "payload": {"devices": [{"id": d["device_id"]} for d in devices]},
        }],
    }
    status_data = await _post_ha(api_key, query, session)
    payload = status_data.get("payload", {}) if isinstance(status_data, dict) else {}
    states = payload.get("devices", {}) if isinstance(payload, dict) else {}
    if not isinstance(states, dict):
        states = {}
    return [_parse_device_status(d, states.get(d["device_id"]) or {}) for d in devices]


async def fetch_poer_status(
    api_key: str,
    preferred_device_id: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    """
    Načte stav jednoho termostatu (preferovaného, jinak prvního na účtu).

    Args:
        api_key:             POER API klíč
        preferred_device_id: Volitelné ID termostatu
        session:             Volitelná sdílená aiohttp session

    Returns:
        dict: Stav termostatu; chyby se vrací v ``error_text`` (nikdy nevyhazuje)
    """
    try:
        statuses = await fetch_poer_statuses(api_key, session)
    except PoerApiError as exc:
        return _empty_status(str(exc))
    except Exception as exc:
        return _empty_status(f"POER neocekavana chyba: {exc}")
    for status in statuses:
        if preferred_device_id and status["device_id"] == str(preferred_device_id):
            return status
    return statuses[0]
```

c) Za funkci `fetch_poer_status_cached` přidej:

```python
async def fetch_poer_statuses_cached(
    api_key: str,
    session: aiohttp.ClientSession | None = None,
    ttl_seconds: float = _STATUS_CACHE_TTL_S,
) -> list[dict[str, Any]]:
    """
    Stav všech termostatů s krátkodobou cache (pro dashboard).

    Args:
        api_key:     POER API klíč
        session:     Volitelná sdílená aiohttp session
        ttl_seconds: Platnost cache v sekundách

    Returns:
        list[dict]: Stav každého termostatu

    Raises:
        PoerApiError: Chyba komunikace
    """
    cache_key = f"{api_key}:*"
    async with _status_cache_lock:
        cached = _status_cache.get(cache_key)
        if cached is not None and (time.monotonic() - cached[0]) < ttl_seconds:
            return cached[1]
    result = await fetch_poer_statuses(api_key, session)
    async with _status_cache_lock:
        _status_cache[cache_key] = (time.monotonic(), result)
    return result
```

d) Celou funkci `send_poer_command` nahraď za:

```python
def _build_execution(endpoint: str, data: dict[str, Any]) -> list[dict[str, Any]] | None:
    """
    Přeloží endpoint a data na ``execution`` pole EXECUTE požadavku.

    POER zpracuje z ``execution`` jen první příkaz – proto vždy jeden.

    Returns:
        list | None: Pole s jedním příkazem, nebo None pro neznámý endpoint
    """
    if endpoint == "set_temp":
        return [{
            "command": "action.devices.commands.ThermostatTemperatureSetpoint",
            "params": {"thermostatTemperatureSetpoint": data["temperature"]},
        }]
    if endpoint == "set_mode":
        mode = str(data.get("mode") or "auto").lower()
        if str(data.get("preset") or "home").lower() == "away":
            mode = "eco"
        return [{
            "command": "action.devices.commands.ThermostatSetMode",
            "params": {"thermostatMode": mode},
        }]
    return None


def _execute_error(response: Any) -> str | None:
    """Vrátí popis chyby, pokud EXECUTE odpověď hlásí jiný status než SUCCESS."""
    payload = response.get("payload", {}) if isinstance(response, dict) else {}
    for command in payload.get("commands", []) if isinstance(payload, dict) else []:
        if command.get("status") != "SUCCESS":
            return f"POER command status {command.get('status')}: {command.get('errorCode', '')}"
    return None


async def send_poer_command(
    api_key: str,
    endpoint: str,
    data: dict[str, Any],
    preferred_device_id: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    """
    Odešle write příkaz do POER cloudu.

    S ``preferred_device_id`` jde rovnou jeden EXECUTE požadavek; bez něj se
    zařízení nejdřív dohledá (první termostat na účtu).

    Args:
        api_key:             POER API klíč
        endpoint:            ``"set_temp"`` nebo ``"set_mode"``
        data:                ``{"temperature": float}`` nebo ``{"mode": str, "preset": str}``
        preferred_device_id: ID termostatu
        session:             Volitelná sdílená aiohttp session

    Returns:
        dict: ``{"success": bool, "device_id": str | None, "error_text": str | None}``
    """
    device_id = str(preferred_device_id) if preferred_device_id else None
    if device_id is None:
        state = await fetch_poer_status(api_key=api_key, session=session)
        device_id = state.get("device_id")
        if not device_id:
            return {
                "success": False,
                "device_id": None,
                "error_text": state.get("error_text") or "POER zarizeni neni dostupne.",
            }

    execution = _build_execution(endpoint, data)
    if execution is None:
        return {
            "success": False,
            "device_id": device_id,
            "error_text": f"Nepodporovany POER prikaz: {endpoint}",
        }

    payload = {
        "requestId": "113",
        "inputs": [{
            "intent": "action.devices.EXECUTE",
            "payload": {"commands": [{"devices": [{"id": device_id}], "execution": execution}]},
        }],
    }
    try:
        response = await _post_ha(api_key, payload, session)
    except PoerApiError as exc:
        return {"success": False, "device_id": device_id, "error_text": str(exc)}

    error_text = _execute_error(response)
    if error_text:
        return {"success": False, "device_id": device_id, "error_text": error_text}

    # Zneplatnit cache stavu (jednotlivé i souhrnné) – frontend po příkazu hned
    # načítá stav a stará hodnota by se ukazovala až 20 s.
    async with _status_cache_lock:
        _status_cache.clear()
    return {"success": True, "device_id": device_id, "error_text": None}
```

e) Pokud `grep -rn "fetch_poer_indoor_temperature" src` najde použití, obnov funkci jako obal:

```python
async def fetch_poer_indoor_temperature(
    api_key: str,
    preferred_device_id: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> float | None:
    """Vrátí aktuální vnitřní teplotu z POER termostatu, nebo None."""
    status = await fetch_poer_status(api_key, preferred_device_id, session)
    return status.get("current_temperature_c")
```

(Pokud použití nenajde, funkci nevracej.)

- [ ] **Step 4: Spusť testy**

Run: `uv run pytest tests/test_poer_api.py -q`
Expected: 12 passed

Run: `uv run pytest tests -q`
Expected: všechny PASS (`test_device_jobs`/`test_web_command_routes` patchují funkce, signatury se nemění).

Run: `uvx flake8 src/poer_api.py tests/test_poer_api.py`
Expected: bez chyb.

- [ ] **Step 5: Commit**

```bash
git add src/poer_api.py tests/test_poer_api.py
git commit -m "Rozšiř POER klienta na více termostatů a omez volání cloudu"
```

---

### Task 2: Nastavení teploty přepne POER do ručního režimu

**Files:**
- Modify: `src/device_jobs.py` (`_poer_skip_reason`, `poer_command_job`)
- Modify: `tests/test_device_jobs.py`

**Interfaces:**
- Consumes (Task 1): `fetch_poer_status`, `send_poer_command` (beze změny signatur).
- Produces: `poer_command_job(api_key, device_id, endpoint, data, check_noop=True)` – pro `set_temp` kroky `set_mode {"mode": "heat", "preset": "home"}` + `set_temp`; krok `set_mode` se vynechá jen při `check_noop=True`, když čerstvý stav je `heat`/`home`.

- [ ] **Step 1: Uprav a doplň testy**

V `tests/test_device_jobs.py` v `PoerJobTests`:

1. V `test_without_noop_check_always_sends` nahraď `send.assert_awaited_once()` za:

```python
        self.assertEqual(
            [c.kwargs["endpoint"] for c in send.await_args_list], ["set_mode", "set_temp"]
        )
        self.assertEqual(send.await_args_list[0].kwargs["data"], {"mode": "heat", "preset": "home"})
```

2. Přidej testy:

```python
    async def test_noop_check_in_auto_mode_switches_to_heat(self) -> None:
        result = {"success": True, "device_id": "p1", "error_text": None}
        status = self._status(mode="auto", target_temperature_c=21.0)
        with patch.object(device_jobs, "fetch_poer_status", AsyncMock(return_value=status)), \
             patch.object(device_jobs, "send_poer_command",
                          AsyncMock(return_value=result)) as send:
            outcome = await poer_command_job("key", "p1", "set_temp", {"temperature": 21.0})()
        self.assertTrue(outcome.sent)
        self.assertEqual(
            [c.kwargs["endpoint"] for c in send.await_args_list], ["set_mode", "set_temp"]
        )

    async def test_noop_check_in_heat_mode_sends_only_temperature(self) -> None:
        result = {"success": True, "device_id": "p1", "error_text": None}
        with patch.object(device_jobs, "fetch_poer_status",
                          AsyncMock(return_value=self._status(mode="heat"))), \
             patch.object(device_jobs, "send_poer_command",
                          AsyncMock(return_value=result)) as send:
            await poer_command_job("key", "p1", "set_temp", {"temperature": 23.0})()
        self.assertEqual([c.kwargs["endpoint"] for c in send.await_args_list], ["set_temp"])

    async def test_failed_mode_switch_stops_before_temperature(self) -> None:
        fail = {"success": False, "device_id": "p1", "error_text": "POER command selhal: 500"}
        with patch.object(device_jobs, "send_poer_command", AsyncMock(return_value=fail)) as send:
            with self.assertRaisesRegex(RuntimeError, "500"):
                await poer_command_job(
                    "key", "p1", "set_temp", {"temperature": 21.0}, check_noop=False
                )()
        send.assert_awaited_once()
```

3. V `tests/test_web_command_routes.py` v `test_manual_poer_command_skips_noop_check` nahraď `send.assert_awaited_once()` za:

```python
        self.assertEqual(send.await_count, 2)
```

- [ ] **Step 2: Spusť testy a ověř, že padají**

Run: `uv run pytest tests/test_device_jobs.py tests/test_web_command_routes.py -q`
Expected: FAIL – `test_without_noop_check_always_sends`, `test_noop_check_in_auto_mode_switches_to_heat` (skip místo odeslání), `test_manual_poer_command_skips_noop_check` (1 != 2).

- [ ] **Step 3: Implementuj**

V `src/device_jobs.py`:

a) V `_poer_skip_reason` nahraď větev `set_temp`:

```python
    if endpoint == "set_temp":
        current = status.get("target_temperature_c")
        if current is not None and abs(float(current) - float(data["temperature"])) < 0.05:
            return f"Cílová teplota POER už je {current} °C."
        return None
```

za:

```python
    if endpoint == "set_temp":
        current = status.get("target_temperature_c")
        in_manual = (status.get("mode"), status.get("preset")) == _POER_MANUAL
        if (in_manual and current is not None
                and abs(float(current) - float(data["temperature"])) < 0.05):
            return f"Cílová teplota POER už je {current} °C."
        return None
```

b) Nad funkci `_poer_requested_state` přidej:

```python
# Ruční režim POER, jak ho hlásí fetch_poer_status. Jen v něm nastavená teplota
# platí trvale – v „auto“ ji vlastní program termostatu při dalším bloku přepíše.
_POER_MANUAL = ("heat", "home")


def _poer_steps(endpoint: str, data: dict, status: dict | None) -> list[tuple[str, dict]]:
    """
    Rozloží POER příkaz na kroky.

    Nastavení teploty se posílá jako ``heat`` + teplota. POER zpracuje v jednom
    požadavku jen první příkaz, proto dva požadavky; přepnutí do ``heat`` na chvíli
    aktivuje uloženou ruční teplotu, kterou druhý krok hned přepíše.

    Args:
        endpoint: ``"set_temp"`` nebo ``"set_mode"``
        data:     Data příkazu
        status:   Čerstvý stav termostatu, nebo None (neznámý – posílá se vše)

    Returns:
        list: Dvojice (endpoint, data) v pořadí odeslání
    """
    if endpoint != "set_temp":
        return [(endpoint, data)]
    steps: list[tuple[str, dict]] = []
    if status is None or (status.get("mode"), status.get("preset")) != _POER_MANUAL:
        steps.append(("set_mode", {"mode": "heat", "preset": "home"}))
    steps.append(("set_temp", data))
    return steps
```

c) V `poer_command_job` nahraď tělo vnitřní funkce `run`:

```python
    async def run() -> CommandOutcome:
        if check_noop:
            # Bez cache – stav starý až 20 s by mohl přeskočit skutečnou změnu.
            status = await fetch_poer_status(api_key=api_key, preferred_device_id=device_id)
            skip_reason = _poer_skip_reason(status, endpoint, data)
            if skip_reason:
                return CommandOutcome(sent=False, skip_reason=skip_reason)
        result = await send_poer_command(
            api_key=api_key, endpoint=endpoint, data=data, preferred_device_id=device_id
        )
        if not result.get("success"):
            raise RuntimeError(result.get("error_text") or "POER příkaz selhal.")
        return CommandOutcome(sent=True, steps=[{"step": endpoint, "result": result}])
```

za:

```python
    async def run() -> CommandOutcome:
        status = None
        if check_noop:
            # Bez cache – stav starý až 20 s by mohl přeskočit skutečnou změnu.
            status = await fetch_poer_status(api_key=api_key, preferred_device_id=device_id)
            skip_reason = _poer_skip_reason(status, endpoint, data)
            if skip_reason:
                return CommandOutcome(sent=False, skip_reason=skip_reason)
            if status.get("error_text"):
                status = None
        steps = []
        for step_endpoint, step_data in _poer_steps(endpoint, data, status):
            result = await send_poer_command(
                api_key=api_key, endpoint=step_endpoint, data=step_data,
                preferred_device_id=device_id,
            )
            if not result.get("success"):
                raise RuntimeError(result.get("error_text") or "POER příkaz selhal.")
            steps.append({"step": step_endpoint, "result": result})
        return CommandOutcome(sent=True, steps=steps)
```

a v docstringu `poer_command_job` doplň pod `check_noop` řádek:

```
                    Nastavení teploty vždy končí v ručním režimu (``heat``).
```

- [ ] **Step 4: Spusť testy**

Run: `uv run pytest tests -q`
Expected: všechny PASS.

Run: `uvx flake8 src/device_jobs.py tests/test_device_jobs.py tests/test_web_command_routes.py`
Expected: bez chyb.

- [ ] **Step 5: Commit**

```bash
git add src/device_jobs.py tests/test_device_jobs.py tests/test_web_command_routes.py
git commit -m "Při nastavení teploty přepni POER do ručního režimu"
```

---

### Task 3: Routy pro výběr termostatu

**Files:**
- Modify: `src/web/routes/poer.py`
- Modify: `tests/test_web_command_routes.py`

**Interfaces:**
- Consumes (Task 1): `fetch_poer_devices`, `fetch_poer_statuses_cached`, `fetch_poer_status_cached`, `PoerApiError`.
- Produces:
  - `GET /api/poer/devices` → `list[dict]` (stavový dict z Task 1 pro každý termostat); 503 při chybě cloudu
  - `GET /api/poer/status?device_id=<id>` (volitelné)
  - Těla `PoerTemperatureRequest` / `PoerModeRequest` mají volitelné `device_id: str | None = None`
  - `async _resolve_device_id(api_key: str, requested: str | None) -> str | None` – 404 pro neznámé ID

- [ ] **Step 1: Napiš padající testy**

V `tests/test_web_command_routes.py`:

1. Do importů přidej `from poer_api import PoerApiError  # noqa: E402`.

2. V `PoerRouteTests.setUp` na konec přidej:

```python
        devices = [{"device_id": "p1", "name": "Kuchobyvak"},
                   {"device_id": "fee89300fac5", "name": "Koupelna"}]
        dev = patch.object(poer, "fetch_poer_devices", AsyncMock(return_value=devices))
        dev.start()
        self.addCleanup(dev.stop)
```

3. Přidej testy:

```python
    async def test_command_for_selected_device(self) -> None:
        arbiter = FakeArbiter(result=CommandOutcome(sent=True))
        body = poer.PoerTemperatureRequest(temperature=24.0, device_id="fee89300fac5")
        await poer.set_poer_temperature(body, _request(arbiter))
        self.assertEqual(arbiter.requests[0].device_key, "poer:fee89300fac5")

    async def test_unknown_device_returns_404(self) -> None:
        arbiter = FakeArbiter(result=CommandOutcome(sent=True))
        body = poer.PoerModeRequest(mode="heat", device_id="cizi")
        with self.assertRaises(HTTPException) as ctx:
            await poer.set_poer_mode(body, _request(arbiter))
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(arbiter.requests, [])

    async def test_status_for_selected_device(self) -> None:
        status = {"device_id": "fee89300fac5"}
        with patch.object(poer, "fetch_poer_status_cached",
                          AsyncMock(return_value=status)) as fetch:
            result = await poer.get_poer_status(device_id="fee89300fac5")
        self.assertEqual(result, status)
        self.assertEqual(fetch.await_args.kwargs["preferred_device_id"], "fee89300fac5")

    async def test_devices_lists_all(self) -> None:
        statuses = [{"device_id": "p1"}, {"device_id": "fee89300fac5"}]
        with patch.object(poer, "fetch_poer_statuses_cached", AsyncMock(return_value=statuses)):
            self.assertEqual(await poer.get_poer_devices(), statuses)

    async def test_devices_cloud_error_returns_503(self) -> None:
        err = PoerApiError("POER SYNC selhal: 500")
        with patch.object(poer, "fetch_poer_statuses_cached", AsyncMock(side_effect=err)):
            with self.assertRaises(HTTPException) as ctx:
                await poer.get_poer_devices()
        self.assertEqual(ctx.exception.status_code, 503)
```

- [ ] **Step 2: Spusť testy a ověř, že padají**

Run: `uv run pytest tests/test_web_command_routes.py -q`
Expected: FAIL – `AttributeError: ... 'fetch_poer_devices'` (setUp) u všech POER testů.

- [ ] **Step 3: Implementuj**

V `src/web/routes/poer.py`:

a) Import `from poer_api import fetch_poer_status_cached` nahraď za:

```python
from poer_api import (
    PoerApiError,
    fetch_poer_devices,
    fetch_poer_status_cached,
    fetch_poer_statuses_cached,
)
```

b) Do `PoerModeRequest` i `PoerTemperatureRequest` přidej pole (a do docstringu `device_id: ID termostatu; bez něj výchozí z konfigurace.`):

```python
    device_id: str | None = None
```

c) Za `_require_poer_api_key` přidej:

```python
async def _resolve_device_id(api_key: str, requested: str | None) -> str | None:
    """
    Vrátí ID cílového termostatu; neznámé ID z prohlížeče odmítne.

    Args:
        api_key:   POER API klíč
        requested: ID z požadavku, nebo None pro výchozí z konfigurace

    Returns:
        str | None: ID termostatu (None = první na účtu)

    Raises:
        HTTPException 404: Termostat s tímto ID na účtu není
        HTTPException 503: Seznam termostatů nelze načíst
    """
    if not requested:
        return _resolve_preferred_device_id()
    try:
        devices = await fetch_poer_devices(api_key)
    except PoerApiError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if requested not in {d["device_id"] for d in devices}:
        raise HTTPException(status_code=404, detail=f"Neznámý POER termostat: {requested}")
    return requested
```

d) V `_submit_poer_command` přidej parametr `requested_device_id: str | None` (za `data`, do docstringu `requested_device_id: ID termostatu z požadavku`) a nahraď řádek `device_id = _resolve_preferred_device_id()` za:

```python
    device_id = await _resolve_device_id(api_key, requested_device_id)
```

e) `get_poer_status` nahraď za:

```python
@router.get("/devices", summary="Stav všech POER termostatů")
async def get_poer_devices() -> list[dict]:
    """Vrátí stav všech POER termostatů na účtu (krátce cachováno)."""

    api_key = _require_poer_api_key()
    try:
        return await fetch_poer_statuses_cached(api_key)
    except PoerApiError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/status", summary="Aktuální stav POER termostatu")
async def get_poer_status(device_id: str | None = None) -> dict:
    """Vrátí stav jednoho POER termostatu (výchozí z konfigurace, krátce cachováno)."""

    api_key = _require_poer_api_key()
    return await fetch_poer_status_cached(
        api_key=api_key,
        preferred_device_id=await _resolve_device_id(api_key, device_id),
    )
```

f) V obou POST endpointech předej `body.device_id`:

```python
    return await _submit_poer_command(
        request, "set_temp", {"temperature": body.temperature}, body.device_id
    )
```

```python
    return await _submit_poer_command(
        request, "set_mode", {"mode": body.mode, "preset": body.preset}, body.device_id
    )
```

- [ ] **Step 4: Spusť testy**

Run: `uv run pytest tests -q`
Expected: všechny PASS.

Run: `uvx flake8 src/web/routes/poer.py tests/test_web_command_routes.py`
Expected: bez chyb.

- [ ] **Step 5: Commit**

```bash
git add src/web/routes/poer.py tests/test_web_command_routes.py
git commit -m "Přidej výběr POER termostatu do API"
```

---

### Task 4: Přepínač termostatu v dashboardu, dokumentace, živé ověření

**Files:**
- Modify: `src/web/templates/dashboard.html`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes (Task 3): `GET /api/poer/devices`, `GET /api/poer/status?device_id=`, `device_id` v tělech POST.

- [ ] **Step 1: Stav a načítání termostatů**

V `dashboard.html` v datech komponenty za `poerSetTemp:     null,` přidej:

```js
        poerDevices:     [],
        poerDeviceId:    null,
```

Za metodu `loadWeatherConfig() { ... },` přidej:

```js
        async loadPoerDevices() {
            try {
                const r = await fetch('/api/poer/devices');
                if (!r.ok) return;
                this.poerDevices = await r.json();
                if (!this.poerDeviceId && this.poerDevices.length) {
                    const preferred = this.weatherConfig?.poer_device_id;
                    const match = this.poerDevices.find(d => d.device_id === preferred);
                    this.poerDeviceId = (match || this.poerDevices[0]).device_id;
                }
            } catch (_) {}
        },

        selectPoerDevice(deviceId) {
            if (deviceId === this.poerDeviceId) return;
            this.poerDeviceId = deviceId;
            this.poerStatus = {};
            this.poerSetTemp = null;
            this.loadPoerStatus();
        },
```

V `loadPoerStatus` nahraď `const r = await fetch('/api/poer/status');` za:

```js
                const query = this.poerDeviceId
                    ? '?device_id=' + encodeURIComponent(this.poerDeviceId) : '';
                const r = await fetch('/api/poer/status' + query);
```

V `applyPoerMode` nahraď `body: JSON.stringify({ mode, preset }),` za `body: JSON.stringify({ mode, preset, device_id: this.poerDeviceId }),` a v `applyPoerTemperature` nahraď `body: JSON.stringify({ temperature: value }),` za `body: JSON.stringify({ temperature: value, device_id: this.poerDeviceId }),`.

V `init()` nahraď řádek `this.loadPoerStatus();` za:

```js
            this.loadPoerDevices().then(() => this.loadPoerStatus());
```

- [ ] **Step 2: Záložní hodnoty jen pro výchozí termostat a oprava rozsahu**

Gettery `poerCurrentTemp` / `poerTargetTemp` berou záložní hodnotu z `weatherConfig` (ta patří výchozímu termostatu). Nahraď v obou řádek `const t = this.weatherConfig?.poer_current_temperature_c;` resp. `..._target_temperature_c;` za:

```js
            if (!this._poerIsDefaultDevice) return null;
            const t = this.weatherConfig?.poer_current_temperature_c;
```

resp.

```js
            if (!this._poerIsDefaultDevice) return null;
            const t = this.weatherConfig?.poer_target_temperature_c;
```

a před `get poerCurrentTemp()` přidej:

```js
        get _poerIsDefaultDevice() {
            return !this.poerDeviceId || this.poerDeviceId === this.weatherConfig?.poer_device_id;
        },
```

Gettery rozsahu četly neexistující pole (`temperature_min_c`/`temperature_max_c`, API vrací `min_temp_c`/`max_temp_c`). Oprav:

```js
        get poerTempMin() {
            const v = this.poerStatus?.min_temp_c;
            return v !== null && v !== undefined ? Number(v) : 5;
        },
        get poerTempMax() {
            const v = this.poerStatus?.max_temp_c;
            return v !== null && v !== undefined ? Number(v) : 35;
        },
```

- [ ] **Step 3: Přepínač v UI**

V bloku `<!-- POER termostat -->` hned za uzavírací `</div>` hlavičky (za tlačítkem `🔄 Obnovit` a jeho `</div>`) vlož:

```html
            <div class="flex gap-2 mb-3" x-show="poerDevices.length > 1">
                <template x-for="d in poerDevices" :key="d.device_id">
                    <button @click="selectPoerDevice(d.device_id)"
                            :disabled="poerLoading"
                            :class="poerDeviceId === d.device_id
                                ? 'bg-blue-600 text-white border-blue-500'
                                : 'bg-gray-700 text-gray-300 border-gray-600 hover:bg-gray-600'"
                            class="flex-1 text-xs px-2 py-1.5 rounded-lg border transition-colors">
                        <span x-text="d.name"></span>
                        <span class="opacity-70"
                              x-text="d.current_temperature_c !== null ? ' ' + d.current_temperature_c + '°C' : ''"></span>
                    </button>
                </template>
            </div>
```

- [ ] **Step 4: CLAUDE.md**

V `CLAUDE.md` v sekci `### Temperature correction / indoor proxy` na konec odstavce doplň větu:

```markdown
POER supports multiple thermostats per account (`GET /api/poer/devices`, optional `device_id` on status/commands; default is `weather.poer_device_id`, else the first thermostat). POER modes: `auto` = thermostat's own program, `heat` = manual (activates the thermostat's stored manual setpoint), `eco` = away. Setting a temperature from the app always sends `heat` first, then the setpoint, as two requests (POER executes only the first command of a multi-command EXECUTE).
```

- [ ] **Step 5: Testy a lint**

Run: `uv run pytest tests -q`
Expected: všechny PASS.

Run: `uvx flake8 src/ --count --select=E9,F63,F7,F82 --statistics`
Expected: `0`.

- [ ] **Step 6: Živé ověření (vyžaduje souhlas uživatele – mění stav termostatu)**

1. Spusť `uv run python src/main.py --mode web`.
2. `curl -s localhost:8000/api/poer/devices` → dva termostaty (Kuchobyvak, Koupelna) s teplotami.
3. Otevři `http://localhost:8000`, záložka POER → přepínač se dvěma termostaty; přepnutí načte stav vybraného.
4. U koupelny nastav teplotu na její aktuální cíl (20 °C) → přes `curl -s "localhost:8000/api/poer/status?device_id=fee89300fac5"` ověř `mode: heat`, `target_temperature_c: 20`.
5. Vrať koupelnu tlačítkem AUTO do programu (pokud si uživatel nepřeje nechat ruční režim).
6. `curl -s -X POST localhost:8000/api/poer/command/set-mode -H "Content-Type: application/json" -d '{"mode":"heat","device_id":"cizi"}'` → HTTP 404.

- [ ] **Step 7: Commit**

```bash
git add src/web/templates/dashboard.html CLAUDE.md
git commit -m "Přidej přepínač POER termostatů do dashboardu"
```
