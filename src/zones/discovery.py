# -*- coding: utf-8 -*-
"""
Návrh zón z nalezených zařízení – výchozí bod editoru zón v aplikaci.

Každý POER termostat dostane vlastní zónu (pojmenovanou podle názvu v POER),
klimatizace patří do první zóny. Uživatel návrh v aplikaci upraví a uloží.
"""

import re
import unicodedata

# Venkovní teplota z ČHMÚ se stahuje jednou za ~3 h – smí být starší než vnitřní čidla.
CHMI_MAX_AGE_MIN = 240


def slugify(text: str) -> str:
    """
    Převede název na ID zóny/čidla (malá písmena bez diakritiky, číslice, ``_``).

    Args:
        text: Název, např. ``"Kuchobývák"``

    Returns:
        str: ID, např. ``"kuchobyvak"``; prázdný výsledek nahradí ``"zona"``
    """
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_text.lower()).strip("_")
    return slug or "zona"


def _unique(slug: str, taken: set[str]) -> str:
    """Vrátí ``slug``, případně s příponou ``_2``, ``_3``… aby se neopakoval."""
    candidate, n = slug, 2
    while candidate in taken:
        candidate, n = f"{slug}_{n}", n + 1
    taken.add(candidate)
    return candidate


def device_sensors(poer_devices: list[dict], lg_devices: list[dict]) -> dict[str, dict]:
    """
    Čidla, která nabízejí nalezená zařízení (+ ČHMÚ).

    Args:
        poer_devices: ``[{"device_id", "name"}]`` z POER SYNC
        lg_devices:   ``[{"device_id", "name"}]`` klimatizací z devices.json

    Returns:
        dict: ID čidla → záznam pro ``zones.json``
    """
    sensors: dict[str, dict] = {}
    taken: set[str] = set()
    for device in poer_devices:
        sensor_id = _unique(f"poer_{slugify(device['name'])}", taken)
        sensors[sensor_id] = {"source": "poer", "device_id": device["device_id"]}
    if lg_devices:
        sensors["lg_klima"] = {"source": "lg"}
    sensors["chmi"] = {"source": "chmi", "max_age_min": CHMI_MAX_AGE_MIN}
    return sensors


def propose_zones(poer_devices: list[dict], lg_devices: list[dict]) -> dict:
    """
    Navrhne obsah ``zones.json`` z nalezených zařízení.

    Args:
        poer_devices: POER termostaty ``[{"device_id", "name"}]``
        lg_devices:   Klimatizace ``[{"device_id", "name"}]``

    Returns:
        dict: Návrh ve formátu ``zones.json``

    Raises:
        ValueError: Nebylo nalezeno žádné zařízení
    """
    if not poer_devices and not lg_devices:
        raise ValueError("Nebylo nalezeno žádné zařízení – zkontrolujte přístupy k LG a POER.")
    sensors = device_sensors(poer_devices, lg_devices)
    poer_sensor = {s["device_id"]: sid for sid, s in sensors.items() if s["source"] == "poer"}
    zones: dict[str, dict] = {}
    taken: set[str] = set()
    for device in poer_devices:
        sensor_id = poer_sensor[device["device_id"]]
        zones[_unique(slugify(device["name"]), taken)] = {
            "name": device["name"],
            "heaters": [{"device": f"poer:{device['device_id']}", "offset_c": 0.0}],
            "roles": {
                "indoor_temperature": [sensor_id],
                "indoor_humidity": [sensor_id],
                "outdoor_temperature": ["chmi"],
            },
        }
    if lg_devices:
        if not zones:
            zones["dum"] = {"name": "Dům", "heaters": [],
                            "roles": {"indoor_temperature": [], "outdoor_temperature": ["chmi"]}}
        first = next(iter(zones.values()))
        first["heaters"].insert(0, {"device": "lg:*", "offset_c": 0.0})
        first["roles"]["indoor_temperature"].append("lg_klima")
    return {"zones": zones, "sensors": sensors}
