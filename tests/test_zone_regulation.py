"""Testy napojení regulace klimatizace na cíl zóny (web/app.py)."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from thermal_controller import ThermalControlPolicy  # noqa: E402
from web import app as web_app  # noqa: E402


class FakeZones:
    """Vrací předem daný cíl zóny pro klimatizaci."""

    def __init__(self, target) -> None:
        self.target = target

    def ac_target(self, device_id: str):
        return self.target


class RegulationPolicyTests(unittest.TestCase):
    def test_without_zone_target_uses_ac_setpoint(self) -> None:
        policy, skip, from_zone = web_app._regulation_policy(
            ThermalControlPolicy(), FakeZones(None), "dev", current_target_c=24.0)
        self.assertEqual(policy.target_temperature_c, 24.0)
        self.assertFalse(skip)
        self.assertFalse(from_zone)

    def test_zone_target_replaces_ac_setpoint(self) -> None:
        policy, skip, from_zone = web_app._regulation_policy(
            ThermalControlPolicy(), FakeZones((21.0, False)), "dev", current_target_c=24.0)
        self.assertEqual(policy.target_temperature_c, 21.0)
        self.assertTrue(from_zone)

    def test_paused_zone_skips_regulation(self) -> None:
        _, skip, _ = web_app._regulation_policy(
            ThermalControlPolicy(), FakeZones((12.0, True)), "dev", current_target_c=24.0)
        self.assertTrue(skip)


class RegulationActiveTests(unittest.TestCase):
    def _app(self, mode: str, dry_run: bool, configured: bool = True):
        zones = SimpleNamespace(control={"mode": mode, "dry_run": dry_run},
                                zones=object() if configured else None)
        return SimpleNamespace(state=SimpleNamespace(zones=zones))

    def test_regulation_runs_only_outside_manual_and_dry_run(self) -> None:
        self.assertTrue(web_app._ac_regulation_active(self._app("automation", False)))
        self.assertTrue(web_app._ac_regulation_active(self._app("program", False)))
        self.assertFalse(web_app._ac_regulation_active(self._app("automation", True)))
        self.assertFalse(web_app._ac_regulation_active(self._app("manual", False)))

    def test_without_zones_file_regulation_ignores_dry_run(self) -> None:
        self.assertTrue(web_app._ac_regulation_active(
            self._app("automation", True, configured=False)))


class JobCorrectionTests(unittest.TestCase):
    def test_room_target_is_corrected_for_ac(self) -> None:
        decision = SimpleNamespace(action="run", mode="HEAT", target_temperature_c=21.0,
                                   wind_strength="AUTO")
        self.assertEqual(web_app._ac_temperature(decision, ThermalControlPolicy(), 2.0), 23.0)
        self.assertEqual(web_app._ac_temperature(decision, ThermalControlPolicy(), 0.0), 21.0)

    def test_missing_decision_target_uses_policy_target(self) -> None:
        decision = SimpleNamespace(target_temperature_c=None)
        policy = ThermalControlPolicy(target_temperature_c=20.0)
        self.assertEqual(web_app._ac_temperature(decision, policy, 1.0), 21.0)


if __name__ == "__main__":
    unittest.main()
