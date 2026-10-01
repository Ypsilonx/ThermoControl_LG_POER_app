# -*- coding: utf-8 -*-
"""
Konfigurace zónového řízení.

- ``data/zones.json`` – struktura domu: zóny, topidla, čidla a role. Mění se výjimečně, ručně.
- ``data/control.json`` – vše, co se mění z aplikace: režim, program, automatika,
  dovolená, přebití a prahy. Ukládá se atomicky.
"""

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MODES = ("manual", "program", "automation", "vacation")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# České názvy dnů pro chybové hlášky.
_DAY_NAMES = dict(zip(WEEKDAYS, ("pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota",
                                 "neděle")))
SENSOR_SOURCES = ("poer", "lg", "chmi", "http", "smart_plug")
HEATER_KINDS = ("poer", "lg")

# Rozsah cílových teplot zadávaných v aplikaci.
TARGET_MIN_C = 5.0
TARGET_MAX_C = 30.0

# Výchozí teploty programu a dovolené – uživatel je pak mění v aplikaci (stránka Řízení).
DEFAULT_COMFORT_C = 21.0
DEFAULT_SETBACK_C = 19.0
DEFAULT_AWAY_C = 15.0

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class Heater:
    """
    Topidlo zóny.

    Args:
        kind:      ``"poer"`` nebo ``"lg"``
        device_id: ID zařízení; None u LG = všechny klimatizace na účtu
        offset_c:  Posun setpointu proti cíli zóny (fólie drží cíl − 1 °C)
    """

    kind: str
    device_id: str | None
    offset_c: float = 0.0


@dataclass(frozen=True)
class Sensor:
    """
    Čidlo v registru.

    Args:
        id:          Identifikátor čidla
        source:      Zdroj hodnot (``SENSOR_SOURCES``)
        device_id:   ID zařízení u ``poer``/``lg`` (None u LG = první klimatizace)
        url:         URL u zdroje ``http``
        max_age_min: Vlastní limit stáří hodnoty; None = globální limit z control.json
    """

    id: str
    source: str
    device_id: str | None = None
    url: str | None = None
    max_age_min: float | None = None


@dataclass(frozen=True)
class Zone:
    """
    Zóna domu.

    Args:
        id:      Identifikátor zóny
        name:    Zobrazovaný název
        heaters: Ovládaná topidla
        roles:   Role → seřazené ID čidel (první čerstvé vyhrává)
    """

    id: str
    name: str
    heaters: tuple[Heater, ...]
    roles: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class ZonesConfig:
    """Zóny a registr čidel ze ``zones.json``."""

    zones: dict[str, Zone]
    sensors: dict[str, Sensor]


def _parse_heater(zone_id: str, raw: dict) -> Heater:
    """Převede ``{"device": "poer:<id>" | "lg:<id>" | "lg:*", "offset_c"?}`` na ``Heater``."""
    kind, _, device_id = str(raw.get("device", "")).partition(":")
    if kind not in HEATER_KINDS or not device_id:
        raise ValueError(f"Zóna {zone_id}: neplatné topidlo '{raw.get('device')}'.")
    return Heater(kind, None if device_id == "*" else device_id, float(raw.get("offset_c", 0.0)))


def _parse_sensor(sensor_id: str, raw: dict) -> Sensor:
    """Převede záznam čidla ze ``zones.json`` na ``Sensor``."""
    source = raw.get("source")
    if source not in SENSOR_SOURCES:
        raise ValueError(f"Čidlo {sensor_id}: neznámý zdroj '{source}'.")
    if source == "http" and not raw.get("url"):
        raise ValueError(f"Čidlo {sensor_id}: zdroj http potřebuje 'url'.")
    max_age = raw.get("max_age_min")
    return Sensor(
        id=sensor_id,
        source=source,
        device_id=raw.get("device_id"),
        url=raw.get("url"),
        max_age_min=float(max_age) if max_age is not None else None,
    )


def parse_zones(raw: dict[str, Any]) -> ZonesConfig:
    """
    Zvaliduje obsah ``zones.json``.

    Args:
        raw: Načtený JSON

    Returns:
        ZonesConfig: Zóny a čidla

    Raises:
        ValueError: Neplatná struktura (neznámé čidlo v roli, zóna bez topidla…)
    """
    sensors = {sid: _parse_sensor(sid, s) for sid, s in (raw.get("sensors") or {}).items()}
    zones: dict[str, Zone] = {}
    for zone_id, z in (raw.get("zones") or {}).items():
        heaters = tuple(_parse_heater(zone_id, h) for h in z.get("heaters") or [])
        if not heaters:
            raise ValueError(f"Zóna {zone_id} nemá žádné topidlo.")
        roles: dict[str, tuple[str, ...]] = {}
        for role, sensor_ids in (z.get("roles") or {}).items():
            for sid in sensor_ids:
                if sid not in sensors:
                    raise ValueError(f"Zóna {zone_id}, role {role}: neznámé čidlo '{sid}'.")
            roles[role] = tuple(sensor_ids)
        zones[zone_id] = Zone(zone_id, str(z.get("name") or zone_id), heaters, roles)
    if not zones:
        raise ValueError("zones.json neobsahuje žádnou zónu.")
    return ZonesConfig(zones, sensors)


def load_zones(path: Path) -> ZonesConfig | None:
    """
    Načte ``zones.json``.

    Args:
        path: Cesta k souboru

    Returns:
        ZonesConfig | None: Konfigurace, nebo None pokud soubor neexistuje

    Raises:
        ValueError: Soubor existuje, ale je neplatný
    """
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name}: neplatný JSON ({exc})") from exc
    return parse_zones(raw)


def _block(start: str, zone_ids: list[str], temperature: float) -> dict:
    """Blok programu se stejnou teplotou pro všechny zóny."""
    return {"from": start, "targets": {z: temperature for z in zone_ids}}


def default_control(zone_ids: list[str], mode: str) -> dict[str, Any]:
    """
    Výchozí ``control.json`` – orientační program, který si uživatel upraví v aplikaci.

    Args:
        zone_ids: ID zón ze ``zones.json``
        mode:     Počáteční režim

    Returns:
        dict: Kompletní konfigurace řízení
    """
    workday = [
        _block("05:30", zone_ids, DEFAULT_COMFORT_C),
        _block("08:00", zone_ids, DEFAULT_SETBACK_C),
        _block("15:00", zone_ids, DEFAULT_COMFORT_C),
        _block("22:00", zone_ids, DEFAULT_SETBACK_C),
    ]
    weekend = [
        _block("07:00", zone_ids, DEFAULT_COMFORT_C),
        _block("22:30", zone_ids, DEFAULT_SETBACK_C),
    ]
    program = {day: json.loads(json.dumps(workday if day not in ("sat", "sun") else weekend))
               for day in WEEKDAYS}
    return {
        "mode": mode,
        # Zkušební provoz: smyčka jen počítá a zapisuje deník, nic neposílá.
        "dry_run": True,
        "emergency_min_c": 12.0,
        "sensor_max_age_min": 15.0,
        "override_hours": 2.0,
        "program": program,
        "automation": {
            "targets": {z: DEFAULT_COMFORT_C for z in zone_ids},
            "night_setback": {"enabled": False, "from": "22:00", "to": "05:30", "delta_c": 2.0},
        },
        "vacation": {
            "return_at": None,
            "away_targets": {z: DEFAULT_AWAY_C for z in zone_ids},
            "preheat_hours": 6.0,
            "previous_mode": "automation",
        },
        "overrides": {},
    }


def _temperature(value: Any, where: str) -> float:
    """Ověří cílovou teplotu v rozsahu ``TARGET_MIN_C``–``TARGET_MAX_C``."""
    try:
        temp = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{where}: teplota '{value}' není číslo.") from None
    if not TARGET_MIN_C <= temp <= TARGET_MAX_C:
        raise ValueError(f"{where}: teplota {temp} °C mimo rozsah {TARGET_MIN_C}–{TARGET_MAX_C}.")
    return temp


def _time(value: Any, where: str) -> str:
    """Ověří čas ve formátu ``HH:MM``."""
    if not isinstance(value, str) or not _TIME_RE.match(value):
        raise ValueError(f"{where}: neplatný čas '{value}' (očekáváno HH:MM).")
    return value


def _targets(raw: Any, zone_ids: list[str], fallback: float, where: str) -> dict[str, float]:
    """Cíle pro všechny zóny; chybějící zóna dostane ``fallback``, neznámá se zahodí."""
    raw = raw if isinstance(raw, dict) else {}
    return {z: _temperature(raw.get(z, fallback), f"{where} ({z})") for z in zone_ids}


def _number(raw: dict, key: str, low: float, high: float) -> float:
    """Číselný parametr v rozsahu ``low``–``high``."""
    try:
        value = float(raw[key])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"{key}: chybí nebo není číslo.") from None
    if not low <= value <= high:
        raise ValueError(f"{key}: {value} mimo rozsah {low}–{high}.")
    return value


def _normalize_program(raw: Any, zone_ids: list[str]) -> dict[str, list[dict]]:
    """Ověří týdenní program a seřadí bloky každého dne podle času."""
    raw = raw if isinstance(raw, dict) else {}
    program = {}
    for day in WEEKDAYS:
        name = _DAY_NAMES[day]
        blocks = raw.get(day)
        if not blocks:
            raise ValueError(f"Program: {name} nemá žádný blok.")
        normalized = [
            {"from": _time(b.get("from"), f"Program {name}"),
             "targets": _targets(b.get("targets"), zone_ids, DEFAULT_COMFORT_C, f"Program {name}")}
            for b in blocks
        ]
        normalized.sort(key=lambda b: b["from"])
        if len({b["from"] for b in normalized}) != len(normalized):
            raise ValueError(f"Program: {name} má dva bloky se stejným časem.")
        program[day] = normalized
    return program


def _normalize_vacation(raw: Any, zone_ids: list[str]) -> dict[str, Any]:
    """Ověří nastavení dovolené."""
    raw = raw if isinstance(raw, dict) else {}
    previous = raw.get("previous_mode") or "automation"
    if previous not in MODES or previous == "vacation":
        raise ValueError(f"Dovolená: neplatný předchozí režim '{previous}'.")
    return {
        "return_at": raw.get("return_at") or None,
        "away_targets": _targets(raw.get("away_targets"), zone_ids, DEFAULT_AWAY_C, "Dovolená"),
        "preheat_hours": _number({**{"preheat_hours": 6.0}, **raw}, "preheat_hours", 0, 72),
        "previous_mode": previous,
    }


def _normalize_overrides(raw: Any, zone_ids: list[str]) -> dict[str, dict]:
    """Ověří přebití; přebití neznámé zóny se zahodí (zóna mohla zmizet ze zones.json)."""
    raw = raw if isinstance(raw, dict) else {}
    return {
        zone: {"target_c": _temperature(o.get("target_c"), f"Přebití {zone}"),
               "until": str(o.get("until"))}
        for zone, o in raw.items()
        if zone in zone_ids and isinstance(o, dict) and o.get("until")
    }


def normalize_control(raw: dict[str, Any], zone_ids: list[str]) -> dict[str, Any]:
    """
    Zvaliduje a doplní ``control.json`` (např. po úpravě z API).

    Args:
        raw:      Konfigurace řízení
        zone_ids: ID zón ze ``zones.json`` – cíle se doplní/ořežou na tyto zóny

    Returns:
        dict: Kompletní, zvalidovaná konfigurace

    Raises:
        ValueError: Neplatná hodnota (zpráva česky, vhodná pro uživatele)
    """
    mode = raw.get("mode")
    if mode not in MODES:
        raise ValueError(f"Neplatný režim '{mode}'.")
    automation = raw.get("automation") if isinstance(raw.get("automation"), dict) else {}
    setback = automation.get("night_setback")
    setback = setback if isinstance(setback, dict) else {}
    vacation = _normalize_vacation(raw.get("vacation"), zone_ids)
    if mode == "vacation" and not vacation["return_at"]:
        raise ValueError("Režim Dovolená potřebuje termín návratu.")
    return {
        "mode": mode,
        "dry_run": bool(raw.get("dry_run", True)),
        "emergency_min_c": _number(raw, "emergency_min_c", 5, 18),
        "sensor_max_age_min": _number(raw, "sensor_max_age_min", 1, 240),
        "override_hours": _number(raw, "override_hours", 0.25, 48),
        "program": _normalize_program(raw.get("program"), zone_ids),
        "automation": {
            "targets": _targets(automation.get("targets"), zone_ids, DEFAULT_COMFORT_C,
                                "Automatika"),
            "night_setback": {
                "enabled": bool(setback.get("enabled", False)),
                "from": _time(setback.get("from", "22:00"), "Noční útlum"),
                "to": _time(setback.get("to", "05:30"), "Noční útlum"),
                "delta_c": _number({"delta_c": 2.0, **setback}, "delta_c", 0, 10),
            },
        },
        "vacation": vacation,
        "overrides": _normalize_overrides(raw.get("overrides"), zone_ids),
    }


def _migrated_mode(state_path: Path) -> str:
    """Režim z dřívějšího ``state.json``: HAND → manual, AUTO → automation, jinak manual."""
    try:
        old = json.loads(state_path.read_text(encoding="utf-8")).get("control_mode")
    except (OSError, ValueError, AttributeError):
        return "manual"
    return {"HAND": "manual", "AUTO": "automation"}.get(old, "manual")


def load_control(path: Path, state_path: Path, zone_ids: list[str]) -> dict[str, Any]:
    """
    Načte ``control.json``; bez něj (nebo když je poškozený) vrátí výchozí konfiguraci.

    Args:
        path:       Cesta k ``control.json``
        state_path: Cesta ke staršímu ``state.json`` (migrace režimu)
        zone_ids:   ID zón ze ``zones.json``

    Returns:
        dict: Zvalidovaná konfigurace řízení
    """
    if path.exists():
        try:
            return normalize_control(json.loads(path.read_text(encoding="utf-8")), zone_ids)
        except (OSError, ValueError) as exc:
            logger.error("❌ %s je neplatný, použity výchozí hodnoty: %s", path.name, exc)
    return default_control(zone_ids, _migrated_mode(state_path))


def save_control(path: Path, control: dict[str, Any]) -> None:
    """
    Atomicky uloží ``control.json`` (zápis do .tmp a přejmenování).

    Args:
        path:    Cesta k ``control.json``
        control: Zvalidovaná konfigurace
    """
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(control, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
