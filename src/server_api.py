# -*- coding: utf-8 -*-
"""
API modul pro komunikaci s LG ThinQ službou.
Poskytuje HTTP přístup ke stavu a ovládání zařízení a real-time
MQTT stream pro okamžité notifikace o změnách stavu bez pollingu.
"""
import json
import logging
import asyncio
import random
import aiohttp
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from thinqconnect import ThinQApi, ThinQAPIException, ThinQMQTTClient

from env_config import load_local_env

logger = logging.getLogger(__name__)
load_local_env()


class ThinQAPI:
    """
    Wrapper nad ThinQ API s podporou HTTP příkazů i real-time MQTT streamu.

    Životní cyklus:
        1. initialize()      – vytvoří HTTP session a ThinQApi objekt
        2. connect_mqtt()    – připojí MQTT pro real-time notifikace (volitelné)
        3. get_device_status() / send_device_command() – HTTP operace
        4. close()           – čistě odpojí MQTT i HTTP session
    """

    def __init__(self):
        self._api: Optional[ThinQApi] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._mqtt: Optional[ThinQMQTTClient] = None
        self.config = self._load_config()

    RETRYABLE_THINQ_ERROR_CODES = {"1306", "2210"}
    RETRYABLE_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}

    # ------------------------------------------------------------------
    # Konfigurace
    # ------------------------------------------------------------------

    def _load_config(self) -> dict:
        """
        Načte konfiguraci přihlašovacích údajů ThinQ.

        Pořadí priorit (vyšší přepisuje nižší):
            1. Proměnné prostředí ``LG_ACCESS_TOKEN``, ``LG_COUNTRY_CODE``,
               ``LG_CLIENT_ID`` – preferováno pro produkci / Docker secrets.
            2. Soubor ``data/config.json`` – vhodné pro lokální vývoj.

        Returns:
            dict: Konfigurační data (access_token, country_code, client_id)

        Raises:
            FileNotFoundError: Pokud chybí env i config.json
            ValueError: Pokud je některá povinná hodnota prázdná nebo placeholder
        """
        import os

        config: dict = {}

        config_path = Path(__file__).parent.parent / "data" / "config.json"
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Nelze načíst config.json (%s); zkusím env.", exc)

        # Proměnné prostředí mají přednost (Docker secrets, .env).
        env_map = {
            "access_token": os.getenv("LG_ACCESS_TOKEN"),
            "country_code": os.getenv("LG_COUNTRY_CODE"),
            "client_id": os.getenv("LG_CLIENT_ID"),
        }
        for key, value in env_map.items():
            if value:
                config[key] = value

        if not config:
            raise FileNotFoundError(
                f"Konfigurace nenalezena: chybí proměnné prostředí i {config_path}. "
                "Zkopírujte data/config.json.example a vyplňte přihlašovací údaje, "
                "nebo nastavte LG_ACCESS_TOKEN, LG_COUNTRY_CODE a LG_CLIENT_ID."
            )

        for key in ("access_token", "country_code", "client_id"):
            value = config.get(key)
            if not value or str(value).startswith("YOUR_"):
                raise ValueError(
                    f"Chybí nebo nevyplněná hodnota '{key}' "
                    "(env proměnná nebo config.json)."
                )
        return config

    # ------------------------------------------------------------------
    # HTTP inicializace
    # ------------------------------------------------------------------

    async def initialize(self) -> ThinQApi:
        """
        Inicializuje HTTP session a ThinQApi objekt (idempotentní).

        Returns:
            ThinQApi: Inicializovaný API objekt
        """
        if self._api is None:
            self._session = aiohttp.ClientSession()
            # Správné pořadí parametrů: session, access_token, country_code, client_id
            self._api = ThinQApi(
                session=self._session,
                access_token=self.config["access_token"],
                country_code=self.config["country_code"],
                client_id=self.config["client_id"],
            )
        return self._api

    def _extract_error_code(self, exc: Exception) -> str | None:
        """
        Vrátí ThinQ chybový kód z výjimky, pokud je k dispozici.

        Args:
            exc: Zachycená výjimka

        Returns:
            str | None: ThinQ kód chyby, jinak None
        """
        code = getattr(exc, "code", None)
        return str(code) if code is not None else None

    def _is_retryable_exception(self, exc: Exception) -> bool:
        """
        Rozhodne, zda je výjimka kandidát na opakování požadavku.

        Args:
            exc: Zachycená výjimka

        Returns:
            bool: True pokud má smysl požadavek opakovat
        """
        if isinstance(exc, ThinQAPIException):
            code = self._extract_error_code(exc)
            return code in self.RETRYABLE_THINQ_ERROR_CODES

        if isinstance(exc, aiohttp.ClientResponseError):
            return exc.status in self.RETRYABLE_HTTP_STATUS_CODES

        if isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError)):
            return True

        return False

    async def _run_with_retry(
        self,
        operation_name: str,
        coroutine_factory: Callable[[], Awaitable[Any]],
        *,
        max_attempts: int = 4,
        base_delay: float = 0.5,
        max_delay: float = 8.0,
    ) -> Any:
        """
        Spustí asynchronní operaci s exponenciálním backoffem a jitterem.

        Retry probíhá pouze pro chyby, které indikují přetížení API nebo
        dočasný síťový problém.

        Args:
            operation_name: Název operace pro logování
            coroutine_factory: Funkce vracející awaitable operaci
            max_attempts: Maximální počet pokusů
            base_delay: Základní čekání mezi pokusy v sekundách
            max_delay: Horní limit čekání mezi pokusy v sekundách

        Returns:
            Any: Výsledek operace

        Raises:
            Exception: Poslední chyba pokud se operace ani po retry nepodaří
        """
        for attempt in range(1, max_attempts + 1):
            try:
                return await coroutine_factory()
            except Exception as exc:
                retryable = self._is_retryable_exception(exc)
                error_code = self._extract_error_code(exc) or "n/a"

                if not retryable or attempt >= max_attempts:
                    logger.error(
                        f"❌ {operation_name} selhalo (pokus {attempt}/{max_attempts}, code={error_code}): {exc}"
                    )
                    raise

                delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                jitter = random.uniform(0.0, delay * 0.35)
                sleep_seconds = delay + jitter

                logger.warning(
                    f"⚠️ {operation_name} selhalo (pokus {attempt}/{max_attempts}, code={error_code}); "
                    f"opakování za {sleep_seconds:.2f}s"
                )
                await asyncio.sleep(sleep_seconds)

    # ------------------------------------------------------------------
    # MQTT real-time stream
    # ------------------------------------------------------------------

    async def connect_mqtt(self, on_message: Callable) -> bool:
        """
        Připojí MQTT klienta pro real-time notifikace o změnách stavu.

        Postup dle ThinQ API specifikace:
            1. GET /route              – zjistí adresu MQTT brokeru
            2. POST /client            – registrace klienta
            3. POST /client/certificate – vydání AWS IoT certifikátu
            4. Připojení na MQTT broker přes mTLS

        Po úspěšném připojení je `on_message` voláno při každé změně
        stavu zařízení – bez jakéhokoliv pollingu.

        Args:
            on_message: Callback volaný při příchodu MQTT zprávy.
                        Signatura: on_message(topic: str, payload: dict, dup: bool,
                                              qos: int, retain: bool, **kwargs)

        Returns:
            bool: True pokud se připojení zdařilo, False jinak
        """
        api = await self.initialize()

        try:
            self._mqtt = ThinQMQTTClient(
                thinq_api=api,
                client_id=self.config["client_id"],
                on_message_received=on_message,
                on_connection_interrupted=self._on_mqtt_interrupted,
                on_connection_success=self._on_mqtt_connected,
                on_connection_failure=self._on_mqtt_failure,
                on_connection_closed=self._on_mqtt_closed,
            )

            # Krok 1: zjistit adresu MQTT serveru přes GET /route
            await self._mqtt.async_init()

            # Krok 2+3: registrace klienta + získání AWS IoT certifikátu
            prepared = await self._mqtt.async_prepare_mqtt()
            if not prepared:
                logger.error("❌ MQTT příprava selhala (certifikát nebo registrace)")
                return False

            # Krok 4: připojení na MQTT broker
            await self._mqtt.async_connect_mqtt()

            if self._mqtt.is_connected:
                logger.info("✅ MQTT připojeno – real-time notifikace aktivní")
                return True
            else:
                logger.error("❌ MQTT připojení neproběhlo")
                return False

        except Exception as e:
            logger.error(f"❌ Chyba při MQTT inicializaci: {e}")
            return False

    def _on_mqtt_connected(self, connection, callback_data, **kwargs):
        """Callback: MQTT úspěšně připojeno.

        AWS CRT SDK předává (connection, callback_data) kde callback_data
        obsahuje atributy return_code a session_present.
        """
        session_present = getattr(callback_data, "session_present", False)
        logger.info(f"📡 MQTT spojení navázáno (session_present={session_present})")

    def _on_mqtt_interrupted(self, connection, error, **kwargs):
        """Callback: MQTT spojení přerušeno – AWS SDK se automaticky pokusí znovu."""
        logger.warning(f"⚠️ MQTT přerušeno: {error} – pokus o reconnect...")

    def _on_mqtt_failure(self, connection, callback_data, **kwargs):
        """Callback: MQTT připojení selhalo."""
        logger.error(f"❌ MQTT selhalo: {callback_data}")

    def _on_mqtt_closed(self, **kwargs):
        """Callback: MQTT spojení ukončeno."""
        logger.info("🔌 MQTT odpojeno")

    @property
    def mqtt_connected(self) -> bool:
        """Vrací True pokud je MQTT aktivní."""
        return self._mqtt is not None and self._mqtt.is_connected

    async def subscribe_device_events(self, device_id: str) -> bool:
        """
        Přihlásí odběr event notifikací pro dané zařízení.

        LG ThinQ platforma posílá přes MQTT notifikace o změně stavu zařízení
        jen pokud je aktivní event subscripce pro daný device_id. Bez tohoto
        volání MQTT doručuje jen systémové zprávy (registrace/odjmutí zařízení),
        nikoli změny stavu z externích zdrojů (LG aplikace v telefonu, ovladač).

        Subscripce vyprší za 4464 hodin (≈ 186 dní). Po restartu serveru
        je třeba volat znovu.

        Args:
            device_id: ThinQ ID zařízení

        Returns:
            bool: True pokud subscripce proběhla úspěšně
        """
        api = await self.initialize()
        try:
            await api.async_post_event_subscribe(device_id)
            logger.info("✅ Event subscripce aktivována pro %s...", device_id[:8])
            return True
        except Exception as exc:
            logger.warning("⚠️ Event subscripce selhala pro %s...: %s", device_id[:8], exc)
            return False

    # ------------------------------------------------------------------
    # HTTP operace se zařízeními
    # ------------------------------------------------------------------

    async def get_device_status(self, device_id: str) -> dict:
        """
        Získá aktuální stav zařízení přes HTTP GET /devices/{deviceId}/state.

        Používá se pro počáteční načtení stavu a po odeslání příkazu.
        Za normálního provozu jsou aktualizace doručovány přes MQTT.

        Args:
            device_id: ID zařízení

        Returns:
            dict: Aktuální stav zařízení

        Raises:
            Exception: Při chybě komunikace s API
        """
        api = await self.initialize()

        async def _request_status() -> dict:
            status = await api.async_get_device_status(device_id)
            logger.debug(f"📋 Stav načten pro {device_id[:8]}...")
            return status

        return await self._run_with_retry(
            "Načítání stavu zařízení",
            _request_status,
            max_attempts=4,
            base_delay=0.4,
        )

    async def send_device_command(self, device_id: str, payload: dict) -> dict:
        """
        Odešle řídicí příkaz zařízení přes HTTP POST /devices/{deviceId}/control.

        Args:
            device_id: ID zařízení
            payload: Řídicí příkaz (viz klima_logic.py)

        Returns:
            dict: Odpověď API

        Raises:
            Exception: Při chybě komunikace nebo odmítnutí příkazu
        """
        api = await self.initialize()
        logger.info(f"📤 Příkaz → {device_id[:8]}...: {json.dumps(payload, ensure_ascii=False)}")

        async def _request_command() -> dict:
            result = await api.async_post_device_control(device_id, payload)
            logger.info(f"📥 Odpověď: {result}")
            return result

        return await self._run_with_retry(
            "Odeslání příkazu zařízení",
            _request_command,
            max_attempts=3,
            base_delay=0.6,
        )

    async def get_devices(self) -> list:
        """
        Načte seznam všech registrovaných zařízení přes GET /devices.

        Returns:
            list: Seznam zařízení
        """
        api = await self.initialize()

        async def _request_devices() -> list:
            return await api.async_get_device_list()

        return await self._run_with_retry(
            "Načítání seznamu zařízení",
            _request_devices,
            max_attempts=4,
            base_delay=0.4,
        )

    async def get_device_profile(self, device_id: str) -> dict:
        """
        Načte profil zařízení přes GET /devices/{deviceId}/profile.

        Profil popisuje dostupné vlastnosti, jejich povolené hodnoty
        a možnosti ovládání. Pokrývá aktuální stav firmware zařízení.

        Args:
            device_id: ID zařízení

        Returns:
            dict: Profil zařízení

        Raises:
            Exception: Při chybě komunikace s API
        """
        api = await self.initialize()

        async def _request_profile() -> dict:
            profile = await api.async_get_device_profile(device_id)
            logger.info(f"📋 Profil zařízení stažen z API ({device_id[:8]}...)")
            return profile

        return await self._run_with_retry(
            "Načítání profilu zařízení",
            _request_profile,
            max_attempts=4,
            base_delay=0.4,
        )

    async def get_energy_usage(
        self,
        device_id: str,
        period: str = "DAILY",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict]:
        """
        Načte data o spotřebě energie přes GET /devices/energy/{deviceId}/usage.

        Energy API není součástí thinqconnect – volá se přímo přes aiohttp session.
        Podporované periody: DAILY (až 31 dní, formát YYYYMMDD),
                             MONTHLY (až 12 měsíců, formát YYYYMM).

        Args:
            device_id: ID zařízení
            period: "DAILY" nebo "MONTHLY"
            start_date: Počáteční datum (výchozí: 7 dní zpět pro DAILY)
            end_date: Koncové datum (výchozí: dnes)

        Returns:
            list[dict]: Seznam záznamů [{"usedDate": "20260511", "energyUsage": 508}, ...]
                        Hodnoty energyUsage jsou v jednotkách Wh.

        Raises:
            Exception: Při chybě komunikace nebo pokud zařízení energy nepodporuje
        """
        from datetime import date, timedelta

        if end_date is None:
            end_date = date.today().strftime("%Y%m%d" if period == "DAILY" else "%Y%m")
        if start_date is None:
            if period == "DAILY":
                start_date = (date.today() - timedelta(days=6)).strftime("%Y%m%d")
            else:
                start_date = (date.today().replace(day=1) - timedelta(days=365 // 12)).strftime("%Y%m")

        # Musíme volat přímo přes session – thinqconnect energy API nepodporuje
        api = await self.initialize()
        url = api._get_url_from_endpoint(f"devices/energy/{device_id}/usage")
        headers = api._generate_headers()
        params = {"period": period, "startDate": start_date, "endDate": end_date}

        async def _request_energy() -> list[dict]:
            if not self._session:
                raise RuntimeError("HTTP session není inicializována")

            async with self._session.get(url, headers=headers, params=params) as resp:
                resp.raise_for_status()
                body = await resp.json()
            data_list = body.get("response", {}).get("result", {}).get("dataList", [])
            logger.info(f"⚡ Energy usage načteno: {len(data_list)} záznamů ({period})")
            return data_list

        return await self._run_with_retry(
            "Načítání energy usage",
            _request_energy,
            max_attempts=4,
            base_delay=0.7,
        )

    # ------------------------------------------------------------------
    # Čistý shutdown
    # ------------------------------------------------------------------

    async def close(self):
        """
        Čistě odpojí MQTT a uzavře HTTP session.

        Volat při vypnutí aplikace pro uvolnění serverových zdrojů
        (zruší registraci klienta přes DELETE /client).
        """
        if self._mqtt and self._mqtt.is_connected:
            try:
                await self._mqtt.async_disconnect()
                logger.info("MQTT odpojeno")
            except Exception as e:
                logger.warning(f"Chyba při odpojení MQTT: {e}")
        self._mqtt = None

        if self._session:
            await self._session.close()
            self._session = None
        self._api = None
        logger.info("API session uzavřena")


# ------------------------------------------------------------------
# Pomocné funkce
# ------------------------------------------------------------------

def _load_devices_file() -> list[dict[str, Any]]:
    """
    Načte obsah data/devices.json a vrátí seznam zařízení.

    Returns:
        list[dict[str, Any]]: Surový seznam zařízení ze souboru

    Raises:
        FileNotFoundError: Pokud soubor devices.json neexistuje
        ValueError: Pokud soubor neobsahuje JSON pole
    """
    devices_path = Path(__file__).parent.parent / "data" / "devices.json"
    try:
        with open(devices_path, "r", encoding="utf-8") as f:
            devices = json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Soubor {devices_path} nenalezen. "
            "Zkopírujte data/devices.json.example a vyplňte Device ID."
        )

    if not isinstance(devices, list):
        raise ValueError("Soubor devices.json musí obsahovat pole zařízení.")

    return devices


def _normalize_device_entry(device: dict[str, Any]) -> dict[str, str | None]:
    """
    Normalizuje záznam zařízení na jednotný tvar napříč starým i novým formátem.

    Args:
        device: Záznam zařízení z devices.json

    Returns:
        dict[str, str | None]: Slovník s klíči device_id, alias, device_type, model_name
    """
    info = device.get("deviceInfo", {}) if isinstance(device.get("deviceInfo"), dict) else {}
    device_id = device.get("deviceId") or device.get("device_id")
    device_type = info.get("deviceType") or device.get("type")
    model_name = info.get("modelName") or device.get("model_name")
    alias = info.get("alias") or device.get("alias") or device_id

    return {
        "device_id": str(device_id) if device_id else None,
        "alias": str(alias) if alias else None,
        "device_type": str(device_type) if device_type else None,
        "model_name": str(model_name) if model_name else None,
    }


def _is_air_conditioner(device_type: str | None) -> bool:
    """
    Ověří, zda typ zařízení odpovídá klimatizaci.

    Args:
        device_type: Typ zařízení z devices.json

    Returns:
        bool: True pokud jde o klimatizaci, jinak False
    """
    if not device_type:
        return False

    normalized = device_type.upper()
    return normalized in {"DEVICE_AIR_CONDITIONER", "AIR_CONDITIONER"}


def list_devices() -> list[dict[str, str | None]]:
    """
    Vrátí seznam zařízení v normalizovaném formátu.

    Returns:
        list[dict[str, str | None]]: Seznam zařízení s jednotnými klíči
    """
    normalized_devices: list[dict[str, str | None]] = []
    for raw_device in _load_devices_file():
        normalized = _normalize_device_entry(raw_device)
        if normalized["device_id"]:
            normalized_devices.append(normalized)
    return normalized_devices


def get_device_id_by_alias(alias: str, prefer_ac: bool = True) -> str:
    """
    Najde Device ID podle aliasu zařízení (case-insensitive).

    Args:
        alias: Alias zařízení (např. "Obývák")
        prefer_ac: Pokud existuje více shod, preferuje klimatizaci

    Returns:
        str: Device ID odpovídající zadanému aliasu

    Raises:
        ValueError: Pokud alias neexistuje nebo není jednoznačný
    """
    alias_norm = alias.strip().lower()
    if not alias_norm:
        raise ValueError("Alias zařízení nesmí být prázdný.")

    matches = [
        d for d in list_devices()
        if d.get("alias") and d["alias"].strip().lower() == alias_norm
    ]

    if not matches:
        raise ValueError(f"Zařízení s aliasem '{alias}' nebylo nalezeno.")

    if prefer_ac:
        ac_match = next((d for d in matches if _is_air_conditioner(d.get("device_type"))), None)
        if ac_match and ac_match.get("device_id"):
            return ac_match["device_id"]

    if len(matches) > 1:
        raise ValueError(
            f"Alias '{alias}' odpovídá více zařízením. "
            "Použijte --device-id pro jednoznačný výběr."
        )

    device_id = matches[0].get("device_id")
    if not device_id:
        raise ValueError(f"Zařízení s aliasem '{alias}' nemá platné device_id.")
    return device_id

def get_ac_device_id() -> str:
    """
    Načte Device ID klimatizace z data/devices.json.
    Hledá první zařízení typu DEVICE_AIR_CONDITIONER.

    Returns:
        str: Device ID klimatizace

    Raises:
        ValueError: Pokud klimatizace v souboru nebyla nalezena
        FileNotFoundError: Pokud soubor devices.json neexistuje
    """
    for device in list_devices():
        if _is_air_conditioner(device.get("device_type")) and device.get("device_id"):
            return device["device_id"]

    raise ValueError(
        "Klimatizace (DEVICE_AIR_CONDITIONER/AIR_CONDITIONER) nebyla nalezena v devices.json."
    )


def list_ac_device_ids() -> list[str]:
    """
    Vrátí Device ID všech klimatizací z data/devices.json.

    Aplikace i plánovač cílí výhradně na klimatizace; ostatní typy
    zařízení (lednice, pračka, ...) jsou v devices.json pouze jako
    příprava pro budoucí rozšíření a tato funkce je vynechává.

    Returns:
        list[str]: Seznam Device ID typu klimatizace (může být prázdný).
    """
    return [
        device["device_id"]
        for device in list_devices()
        if _is_air_conditioner(device.get("device_type")) and device.get("device_id")
    ]
