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
SUN_SIDES = ("east", "west")

# Rozsah cílových teplot zadávaných v aplikaci.
TARGET_MIN_C = 5.0
TARGET_MAX_C = 30.0

# Výchozí teploty programu a dovolené – uživatel je pak mění v aplikaci (stránka Řízení).
DEFAULT_COMFORT_C = 21.0
DEFAULT_SETBACK_C = 19.0
DEFAULT_AWAY_C = 15.0

# Výchozí volba zdroje a tarif (spec kap. 6–7). Uživatel je mění v aplikaci (Řízení → Nastavení).
DEFAULT_SOURCES = {
    # Orientační okno NT, dokud uživatel nezadá skutečné časy HDO.
    "tariff": {"workday": [{"from": "22:00", "to": "06:00"}],
               "weekend": [{"from": "22:00", "to": "06:00"}]},
    # Pod touto venkovní teplotou ztrácí AC výkon – fólie topí jako záloha i ve VT.
    "ac_min_outdoor_c": -5.0,
    # Předpověď mrazu → fólie v NT nahřívá na plný cíl (do zásoby).
    "frost": {"enabled": True, "threshold_c": 0.0, "hours": 12},
    # AC nestačí: topí aspoň `minutes` a teplota za tu dobu klesla o `drop_c`.
    "ac_insufficient": {"minutes": 30, "drop_c": 0.3},
}
DEFAULT_SUN = {"enabled": True, "reduction_c": 0.5, "max_cloudiness_pct": 40.0, "lead_hours": 2.0}
DEFAULT_DRYING = {"enabled": True, "humidity_pct": 70.0, "rearm_pct": 65.0, "boost_c": 1.0,
                  "max_minutes": 60.0}
# Souřadnice domu pro východ/západ slunce (výchozí Valašské Meziříčí).
DEFAULT_LOCATION = {"lat": 49.47, "lon": 17.97}

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# ID zóny a čidla: klíč v control.json a v URL (/api/control/override/<id>).
_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")


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
        roles:    Role → seřazené ID čidel (první čerstvé vyhrává)
        sun_side: Strana domu pro úpravu cíle podle slunce (``east``/``west``) nebo None
    """

    id: str
    name: str
    heaters: tuple[Heater, ...]
    roles: dict[str, tuple[str, ...]]
    sun_side: str | None = None


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


def _check_id(kind: str, value: str) -> None:
    """Ověří ID zóny/čidla (malá písmena bez diakritiky, číslice, ``_``)."""
    if not _ID_RE.match(value):
        raise ValueError(f"{kind} '{value}': ID smí obsahovat jen malá písmena a-z, číslice a _.")


def _parse_sensor(sensor_id: str, raw: dict) -> Sensor:
    """Převede záznam čidla ze ``zones.json`` na ``Sensor``."""
    _check_id("Čidlo", sensor_id)
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
        _check_id("Zóna", zone_id)
        heaters = tuple(_parse_heater(zone_id, h) for h in z.get("heaters") or [])
        if not heaters:
            raise ValueError(f"Zóna {zone_id} nemá žádné topidlo.")
        roles: dict[str, tuple[str, ...]] = {}
        for role, sensor_ids in (z.get("roles") or {}).items():
            for sid in sensor_ids:
                if sid not in sensors:
                    raise ValueError(f"Zóna {zone_id}, role {role}: neznámé čidlo '{sid}'.")
            roles[role] = tuple(sensor_ids)
        sun_side = z.get("sun_side") or None
        if sun_side is not None and sun_side not in SUN_SIDES:
            raise ValueError(f"Zóna {zone_id}: neznámá strana '{sun_side}' (east/west).")
        zones[zone_id] = Zone(zone_id, str(z.get("name") or zone_id), heaters, roles, sun_side)
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


def read_zones_raw(path: Path) -> dict | None:
    """
    Načte ``zones.json`` bez validace (pro editor v aplikaci).

    Returns:
        dict | None: Obsah souboru, nebo None když chybí či není čitelný JSON
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_zones(path: Path, raw: dict[str, Any]) -> ZonesConfig:
    """
    Zvaliduje a atomicky uloží ``zones.json``; neplatná konfigurace se neuloží.

    Args:
        path: Cesta k ``zones.json``
        raw:  Nový obsah

    Returns:
        ZonesConfig: Uložená konfigurace

    Raises:
        ValueError: Neplatná konfigurace
    """
    config = parse_zones(raw)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return config


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
            "sun": dict(DEFAULT_SUN),
            "drying": dict(DEFAULT_DRYING),
        },
        "vacation": {
            "return_at": None,
            "away_targets": {z: DEFAULT_AWAY_C for z in zone_ids},
            "preheat_hours": 6.0,
            "previous_mode": "automation",
        },
        "overrides": {},
        "sources": json.loads(json.dumps(DEFAULT_SOURCES)),
        "location": dict(DEFAULT_LOCATION),
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


def _with_defaults(raw: Any, defaults: dict) -> dict:
    """Slovník ``raw`` doplněný o chybějící klíče z ``defaults`` (starší soubory)."""
    return {**defaults, **(raw if isinstance(raw, dict) else {})}


def _normalize_tariff(raw: Any) -> dict[str, list[dict]]:
    """Ověří okna NT pro pracovní den a víkend."""
    tariff = _with_defaults(raw, DEFAULT_SOURCES["tariff"])
    result = {}
    for day_type, label in (("workday", "pracovní den"), ("weekend", "víkend")):
        windows = []
        for w in tariff[day_type] or []:
            start = _time(w.get("from"), f"Tarif NT ({label})")
            end = _time(w.get("to"), f"Tarif NT ({label})")
            if start == end:
                raise ValueError(f"Tarif NT ({label}): okno {start}–{end} nemá délku.")
            windows.append({"from": start, "to": end})
        result[day_type] = windows
    return result


def _normalize_sources(raw: Any) -> dict[str, Any]:
    """Ověří tarif a prahy volby zdroje (fólie jako záloha AC)."""
    sources = _with_defaults(raw, DEFAULT_SOURCES)
    frost = _with_defaults(sources["frost"], DEFAULT_SOURCES["frost"])
    insufficient = _with_defaults(sources["ac_insufficient"], DEFAULT_SOURCES["ac_insufficient"])
    return {
        "tariff": _normalize_tariff(sources["tariff"]),
        "ac_min_outdoor_c": _number(sources, "ac_min_outdoor_c", -30, 15),
        "frost": {
            "enabled": bool(frost["enabled"]),
            "threshold_c": _number(frost, "threshold_c", -20, 10),
            "hours": _number(frost, "hours", 1, 48),
        },
        "ac_insufficient": {
            "minutes": _number(insufficient, "minutes", 10, 180),
            "drop_c": _number(insufficient, "drop_c", 0.1, 3),
        },
    }


def _normalize_sun(raw: Any) -> dict[str, Any]:
    """Ověří úpravu cíle podle slunce."""
    sun = _with_defaults(raw, DEFAULT_SUN)
    return {
        "enabled": bool(sun["enabled"]),
        "reduction_c": _number(sun, "reduction_c", 0, 3),
        "max_cloudiness_pct": _number(sun, "max_cloudiness_pct", 0, 100),
        "lead_hours": _number(sun, "lead_hours", 0, 6),
    }


def _normalize_drying(raw: Any) -> dict[str, Any]:
    """Ověří vysoušení koupelny (návrat musí být pod prahem, jinak by se spínalo dokola)."""
    drying = _with_defaults(raw, DEFAULT_DRYING)
    result = {
        "enabled": bool(drying["enabled"]),
        "humidity_pct": _number(drying, "humidity_pct", 40, 100),
        "rearm_pct": _number(drying, "rearm_pct", 30, 100),
        "boost_c": _number(drying, "boost_c", 0, 5),
        "max_minutes": _number(drying, "max_minutes", 5, 240),
    }
    if result["rearm_pct"] >= result["humidity_pct"]:
        raise ValueError("Vysoušení: vlhkost pro opětovné zapnutí musí být pod prahem.")
    return result


def _normalize_location(raw: Any) -> dict[str, float]:
    """Ověří souřadnice domu."""
    location = _with_defaults(raw, DEFAULT_LOCATION)
    return {"lat": _number(location, "lat", -90, 90), "lon": _number(location, "lon", -180, 180)}


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
            "sun": _normalize_sun(automation.get("sun")),
            "drying": _normalize_drying(automation.get("drying")),
        },
        "vacation": vacation,
        "overrides": _normalize_overrides(raw.get("overrides"), zone_ids),
        "sources": _normalize_sources(raw.get("sources")),
        "location": _normalize_location(raw.get("location")),
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
