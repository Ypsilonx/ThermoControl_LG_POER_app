"""Testy konfigurace zón (zones.json) a řízení (control.json)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from zones.config import (  # noqa: E402
    WEEKDAYS,
    default_control,
    load_control,
    load_zones,
    normalize_control,
    parse_zones,
    save_control,
)

ZONES_RAW = {
    "zones": {
        "kuchoobyvak": {
            "name": "Kuchoobývák",
            "heaters": [{"device": "lg:*"}, {"device": "poer:p1", "offset_c": -1.0}],
            "roles": {"indoor_temperature": ["poer_k", "lg"], "fireplace": ["plug"]},
        },
        "koupelna": {
            "name": "Koupelna",
            "heaters": [{"device": "poer:p2"}],
            "roles": {"indoor_temperature": ["poer_b"]},
        },
    },
    "sensors": {
        "poer_k": {"source": "poer", "device_id": "p1"},
        "poer_b": {"source": "poer", "device_id": "p2"},
        "lg": {"source": "lg"},
        "chmi": {"source": "chmi", "max_age_min": 240},
        "plug": {"source": "smart_plug"},
    },
}
ZONE_IDS = ["kuchoobyvak", "koupelna"]


class ZonesConfigTests(unittest.TestCase):
    def test_parse_zones(self) -> None:
        cfg = parse_zones(ZONES_RAW)
        kitchen = cfg.zones["kuchoobyvak"]
        self.assertEqual(kitchen.name, "Kuchoobývák")
        self.assertEqual([h.kind for h in kitchen.heaters], ["lg", "poer"])
        self.assertEqual(kitchen.heaters[1].device_id, "p1")
        self.assertEqual(kitchen.heaters[1].offset_c, -1.0)
        self.assertIsNone(kitchen.heaters[0].device_id)
        self.assertEqual(kitchen.roles["indoor_temperature"], ("poer_k", "lg"))
        self.assertEqual(cfg.sensors["chmi"].max_age_min, 240)

    def test_unknown_sensor_in_role_is_error(self) -> None:
        raw = json.loads(json.dumps(ZONES_RAW))
        raw["zones"]["koupelna"]["roles"]["indoor_temperature"] = ["neni"]
        with self.assertRaisesRegex(ValueError, "neni"):
            parse_zones(raw)

    def test_zone_without_heater_is_error(self) -> None:
        raw = json.loads(json.dumps(ZONES_RAW))
        raw["zones"]["koupelna"]["heaters"] = []
        with self.assertRaisesRegex(ValueError, "koupelna"):
            parse_zones(raw)

    def test_unknown_sensor_source_is_error(self) -> None:
        raw = json.loads(json.dumps(ZONES_RAW))
        raw["sensors"]["lg"]["source"] = "zigbee"
        with self.assertRaisesRegex(ValueError, "zigbee"):
            parse_zones(raw)

    def test_missing_zones_file_returns_none(self) -> None:
        self.assertIsNone(load_zones(Path(tempfile.gettempdir()) / "neexistuje_zones.json"))

    def test_example_template_is_valid(self) -> None:
        cfg = load_zones(ROOT / "data" / "zones.json.example")
        self.assertEqual(set(cfg.zones), {"kuchoobyvak", "koupelna"})


class ControlConfigTests(unittest.TestCase):
    def test_default_has_program_for_every_day_and_zone(self) -> None:
        control = default_control(ZONE_IDS, "manual")
        self.assertEqual(control["mode"], "manual")
        self.assertTrue(control["dry_run"])
        self.assertEqual(control["emergency_min_c"], 12.0)
        self.assertEqual(set(control["program"]), set(WEEKDAYS))
        for blocks in control["program"].values():
            self.assertTrue(blocks)
            for block in blocks:
                self.assertEqual(set(block["targets"]), set(ZONE_IDS))
        self.assertEqual(set(control["automation"]["targets"]), set(ZONE_IDS))
        self.assertEqual(set(control["vacation"]["away_targets"]), set(ZONE_IDS))

    def test_normalize_fills_missing_zone_targets_and_sorts_blocks(self) -> None:
        raw = default_control(["kuchoobyvak"], "program")
        raw["program"]["mon"] = [{"from": "15:00", "targets": {"kuchoobyvak": 22}},
                                 {"from": "06:00", "targets": {"kuchoobyvak": 21}}]
        control = normalize_control(raw, ZONE_IDS)
        self.assertEqual([b["from"] for b in control["program"]["mon"]], ["06:00", "15:00"])
        self.assertIn("koupelna", control["program"]["mon"][0]["targets"])
        self.assertIn("koupelna", control["automation"]["targets"])

    def test_invalid_values_are_rejected(self) -> None:
        cases = [
            ("mode", "turbo"),
            ("emergency_min_c", 2),
            ("override_hours", 0),
        ]
        for key, value in cases:
            raw = default_control(ZONE_IDS, "manual")
            raw[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                normalize_control(raw, ZONE_IDS)

    def test_invalid_block_time_and_target_are_rejected(self) -> None:
        for block in ({"from": "25:00", "targets": {}}, {"from": "06:00",
                                                         "targets": {"koupelna": 45}}):
            raw = default_control(ZONE_IDS, "manual")
            raw["program"]["tue"] = [block]
            with self.subTest(block=block), self.assertRaises(ValueError):
                normalize_control(raw, ZONE_IDS)

    def test_empty_day_is_rejected(self) -> None:
        raw = default_control(ZONE_IDS, "manual")
        raw["program"]["wed"] = []
        with self.assertRaisesRegex(ValueError, "středa"):
            normalize_control(raw, ZONE_IDS)

    def test_vacation_mode_requires_return_time(self) -> None:
        raw = default_control(ZONE_IDS, "vacation")
        with self.assertRaisesRegex(ValueError, "návrat"):
            normalize_control(raw, ZONE_IDS)

    def test_override_for_unknown_zone_is_dropped(self) -> None:
        raw = default_control(ZONE_IDS, "program")
        raw["overrides"] = {"puda": {"target_c": 20, "until": "2026-10-01T10:00:00"},
                            "koupelna": {"target_c": 23, "until": "2026-10-01T10:00:00"}}
        control = normalize_control(raw, ZONE_IDS)
        self.assertEqual(list(control["overrides"]), ["koupelna"])


class ControlFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "control.json"
        self.state = Path(self.dir.name) / "state.json"

    def test_migrates_mode_from_state_json(self) -> None:
        for old, new in (("HAND", "manual"), ("AUTO", "automation")):
            self.state.write_text(json.dumps({"control_mode": old}), encoding="utf-8")
            with self.subTest(old=old):
                self.assertEqual(load_control(self.path, self.state, ZONE_IDS)["mode"], new)

    def test_without_any_file_starts_in_manual(self) -> None:
        self.assertEqual(load_control(self.path, self.state, ZONE_IDS)["mode"], "manual")

    def test_save_and_load_roundtrip(self) -> None:
        control = default_control(ZONE_IDS, "program")
        control["emergency_min_c"] = 13.0
        save_control(self.path, control)
        loaded = load_control(self.path, self.state, ZONE_IDS)
        self.assertEqual(loaded["mode"], "program")
        self.assertEqual(loaded["emergency_min_c"], 13.0)
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())

    def test_corrupt_file_falls_back_to_defaults(self) -> None:
        self.path.write_text("{nejde", encoding="utf-8")
        self.assertEqual(load_control(self.path, self.state, ZONE_IDS)["mode"], "manual")


if __name__ == "__main__":
    unittest.main()
