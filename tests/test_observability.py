"""Offline sensor and cached-metrics tests: no hardware or network calls."""
import contextlib
import dataclasses
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import nvidia_idrac_fan as fan
import physical_telemetry as physical
import status_metrics as metrics
from test_fan import FakeIDRAC, config, gpu


SENSORS = """System Board Inlet Temp       Ok      24C      -7C  47C      3C [Y]      42C [Y]
System Board Exhaust Temp     Ok      32C      0C   75C      0C [N]      70C [N]
CPU1 Temp                     Ok      44C      3C   103C     8C [N]      98C [N]
CPU2 Temp                     Ok      46C      3C   103C     8C [N]      98C [N]
System Board Fan1               Ok          10440RPM    360RPM      NA          55%
System Board Fan3               Ok          10320RPM    360RPM      NA          55%
"""


class PhysicalParserTests(unittest.TestCase):
    def test_actual_reading_is_not_a_threshold_and_inventory_is_generic(self):
        parsed = physical.parse_chassis_sensors(SENSORS)
        self.assertEqual([f["fan_id"] for f in parsed["fans"]], [1, 3])
        self.assertEqual(parsed["fans"][0], dict(fan_id=1, health="ok", rpm=10440, pwm_pct=55))
        self.assertEqual({(t["kind"], t["index"]): t["temperature_c"] for t in parsed["temperatures"]},
                         {("inlet", 0): 24, ("exhaust", 0): 32, ("cpu", 1): 44, ("cpu", 2): 46})

    def test_missing_unknown_and_malformed_values_are_never_zero(self):
        for text in (SENSORS.replace("10440RPM", "N/A"), SENSORS.replace("55%", "NA"), SENSORS.replace("10440RPM", "999999RPM")):
            parsed = physical.parse_chassis_sensors(text)
            self.assertTrue(parsed["issues"])
            result = physical.assess_snapshot(parsed, physical.DiagnosticsConfig(expected_fan_ids=[1, 3]),
                                              age_seconds=0, requested_floor=55, last_read_ok=True)
            self.assertIsNone(result["fan_pwm_verified"])
        empty = physical.parse_chassis_sensors("unsupported or empty output")
        self.assertEqual(empty["fans"], [])
        self.assertIn("fan-telemetry-unavailable", empty["issues"])

    def test_duplicate_sensor_rows_cannot_establish_proof(self):
        parsed = physical.parse_chassis_sensors(SENSORS + SENSORS)
        self.assertIn("duplicate-fan-row", parsed["issues"])
        self.assertIsNone(parsed["fans"][0]["rpm"])
        self.assertTrue(all(t["temperature_c"] is None for t in parsed["temperatures"]))

    def test_explicit_inventory_detects_missing_or_extra_fans(self):
        parsed = physical.parse_chassis_sensors(SENSORS)
        value = physical.assess_snapshot(parsed, physical.DiagnosticsConfig(expected_fan_ids=[1, 2, 3]),
                                         age_seconds=0, requested_floor=55, last_read_ok=True)
        self.assertEqual(value["missing_fan_ids"], [2])
        self.assertFalse(value["inventory_complete_verified"])
        self.assertFalse(value["fan_pwm_verified"])

    def test_discovery_is_not_whole_inventory_or_rpm_sufficiency_proof(self):
        value = physical.assess_snapshot(physical.parse_chassis_sensors(SENSORS), physical.DiagnosticsConfig(),
                                         age_seconds=0, requested_floor=55, last_read_ok=True)
        self.assertEqual(value["inventory_scope"], "discovered-only")
        self.assertFalse(value["inventory_complete_verified"])
        self.assertIsNone(value["fan_pwm_verified"])
        self.assertIsNone(value["fan_rpm_verified"])

    def test_stale_or_failed_last_read_invalidates_old_proofs(self):
        c = physical.DiagnosticsConfig(expected_fan_ids=[1, 3])
        for age, last_read_ok in ((91, True), (-1, True), (0, False)):
            value = physical.assess_snapshot(physical.parse_chassis_sensors(SENSORS), c,
                                             age_seconds=age, requested_floor=55, last_read_ok=last_read_ok)
            self.assertFalse(value["fresh"])
            self.assertIsNone(value["fan_pwm_verified"])
            self.assertIsNone(value["fan_rpm_verified"])

    def test_approved_per_fan_envelope_detects_slow_fan_without_averaging(self):
        c = physical.DiagnosticsConfig(expected_fan_ids=[1, 3], rpm_calibration_approved=True,
                                       rpm_minimums={"1": [[55, 10000], [84, 14000]], "3": [[55, 10000], [84, 14000]]})
        c.validate()
        value = physical.assess_snapshot(physical.parse_chassis_sensors(SENSORS), c, age_seconds=0, requested_floor=55, last_read_ok=True)
        self.assertTrue(value["fan_pwm_verified"])
        self.assertTrue(value["fan_rpm_verified"])
        value = physical.assess_snapshot(physical.parse_chassis_sensors(SENSORS.replace("10320RPM", "400RPM")), c,
                                         age_seconds=0, requested_floor=55, last_read_ok=True)
        self.assertFalse(value["fan_rpm_verified"])

    def test_envelope_is_unknown_below_its_calibrated_range(self):
        c = physical.DiagnosticsConfig(expected_fan_ids=[1, 3], rpm_calibration_approved=True,
                                       rpm_minimums={"1": [[84, 14000]], "3": [[84, 14000]]})
        value = physical.assess_snapshot(physical.parse_chassis_sensors(SENSORS), c, age_seconds=0, requested_floor=55, last_read_ok=True)
        self.assertIsNone(value["fan_rpm_verified"])

    def test_bad_diagnostic_configuration_rejected(self):
        invalid = [dict(enabled="yes"), dict(interval_seconds=0), dict(max_age_seconds=1),
                   dict(expected_fan_ids=[True]), dict(expected_fan_ids=[1, 1]),
                   dict(rpm_minimums={"1": [[55, 10000]]}),
                   dict(expected_fan_ids=[1], rpm_calibration_approved=True, rpm_minimums={"1": [[55, 10000], [84, 9000]]})]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(physical.DiagnosticError):
                physical.DiagnosticsConfig(**values).validate()


class DiagnosticControllerTests(unittest.TestCase):
    def controller(self, reader):
        idrac = FakeIDRAC()
        idrac.read_sensors = reader
        return fan.Controller(dataclasses.replace(config(), diagnostics=physical.DiagnosticsConfig(expected_fan_ids=[1, 3])),
                              idrac, apply=True, telemetry=lambda: [gpu(), gpu("GPU-synthetic-b")])

    def test_diagnostics_after_control_and_separate_proof(self):
        c = self.controller(lambda: physical.parse_chassis_sensors(SENSORS))
        result = c.step(0)
        self.assertTrue(result["floor_setting_verified"])
        self.assertFalse(result["fan_pwm_verified"])  # 55 measured, 100 requested
        self.assertIsNone(result["fan_rpm_verified"])
        self.assertFalse(result["fault"])
        self.assertEqual(result["cycle_sequence"], 1)
        self.assertGreater(result["sample_monotonic"], 0)

    def test_diagnostic_command_failure_does_not_fault_control_or_expose_error(self):
        def failure():
            raise fan.ControlError("secret address or command details")
        c = self.controller(failure)
        result = c.step(0)
        self.assertTrue(result["floor_setting_verified"])
        self.assertFalse(result["fault"])
        self.assertFalse(result["physical_diagnostics"]["fresh"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(c.idrac.writes, [100])

    def test_failed_refresh_retains_measurements_but_invalidates_proof(self):
        c = self.controller(lambda: physical.parse_chassis_sensors(SENSORS))
        c.step(0)
        c.physical_attempt_monotonic = None
        c.idrac.read_sensors = lambda: (_ for _ in ()).throw(fan.ControlError("private"))
        c.step(5)
        result = c.status([], 100, "test", False)
        self.assertTrue(result["physical_diagnostics"]["available"])
        self.assertFalse(result["physical_diagnostics"]["fresh"])
        self.assertEqual(len(result["physical_diagnostics"]["fans"]), 2)

    def test_disabled_diagnostics_make_no_read_and_never_write_additional_properties(self):
        c = self.controller(lambda: self.fail("diagnostics should be disabled"))
        c.config = dataclasses.replace(c.config, diagnostics=physical.DiagnosticsConfig(enabled=False))
        self.assertFalse(c.step(0)["physical_diagnostics"]["enabled"])
        self.assertEqual(c.idrac.writes, [100])

    def test_slow_sensor_response_is_aged_from_request_start(self):
        clock = {"monotonic": 100, "wall": 1000}
        def slow_read():
            clock.update(monotonic=130, wall=1030)
            return physical.parse_chassis_sensors(SENSORS)
        c = self.controller(slow_read)
        with patch("nvidia_idrac_fan.time.monotonic", side_effect=lambda: clock["monotonic"]), \
             patch("nvidia_idrac_fan.time.time", side_effect=lambda: clock["wall"]):
            c.refresh_diagnostics()
            result = c.status([], 55, "test", False)
        self.assertEqual(result["physical_diagnostics"]["observed_time_unix"], 1000)
        self.assertEqual(result["physical_diagnostics"]["age_seconds"], 30)

    def test_producer_identity_distinguishes_same_boot_invocations(self):
        first = self.controller(lambda: physical.parse_chassis_sensors(SENSORS))
        second = self.controller(lambda: physical.parse_chassis_sensors(SENSORS))
        self.assertRegex(first.producer_instance, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first.producer_instance, second.producer_instance)
        self.assertEqual(first.status([], 55, "test", False)["producer_instance"], first.producer_instance)


def cached():
    return {"schema": 1, "time_unix": 1000, "sample_monotonic": 100, "controller_running": True,
            "control_verified": True, "floor_setting_verified": True, "fault": False,
            "requested_floor_pct": 55, "verified_floor_pct": 55,
            "reason": "private-host", "host": "private-host", "raw_error": "private-secret",
            "physical_diagnostics": {"fresh": True, "age_seconds": 2, "observed_time_unix": 998,
                "fan_pwm_verified": True, "fan_rpm_verified": None,
                "fans": [{"fan_id": 3, "rpm": 10320, "pwm_pct": 55, "health": "ok"}],
                "temperatures": [{"kind": "inlet", "index": 0, "temperature_c": 24}]},
            "gpus": [{"index_in_sample": 0, "core_c": 42, "memory_c": 43, "power_w": 25,
                      "utilization_pct": 0, "uuid": "GPU-private"}]}


class CachedMetricsTests(unittest.TestCase):
    def test_allowlist_drops_private_values_and_has_fixed_numeric_labels(self):
        value = metrics.normalize_status(cached(), now=1005, max_age=90)
        rendered = metrics.prometheus(value)
        self.assertNotIn("private", json.dumps(value) + rendered)
        self.assertIn('nvidia_idrac_fan_rpm{fan="3"} 10320', rendered)
        self.assertIn("nvidia_idrac_fan_rpm_verified_known 0", rendered)
        self.assertTrue(value["fan_pwm_verified"])

    def test_stale_cached_booleans_do_not_remain_verified(self):
        value = metrics.normalize_status(cached(), now=1100, max_age=90)
        self.assertFalse(value["status_fresh"])
        self.assertFalse(value["floor_setting_verified"])
        self.assertFalse(value["physical_fresh"])
        self.assertIsNone(value["fan_pwm_verified"])

    def test_physical_age_includes_time_since_status_publication(self):
        raw = cached()
        raw["physical_diagnostics"]["age_seconds"] = 89
        self.assertFalse(metrics.normalize_status(raw, now=1005, max_age=90)["physical_fresh"])

    def test_source_shorter_ttl_expires_before_exporter_global_ttl(self):
        raw = cached()
        raw["physical_diagnostics"]["age_seconds"] = 8
        raw["physical_diagnostics"]["max_age_seconds"] = 10
        self.assertTrue(metrics.normalize_status(raw, now=1001, max_age=90)["fan_pwm_verified"])
        value = metrics.normalize_status(raw, now=1005, max_age=90)
        self.assertTrue(value["status_fresh"])
        self.assertFalse(value["physical_fresh"])
        self.assertIsNone(value["fan_pwm_verified"])

    def test_same_boot_monotonic_age_survives_wall_clock_change(self):
        raw = cached()
        boot = "00000000-0000-0000-0000-000000000001"
        raw["boot_id"] = boot
        value = metrics.normalize_status(raw, now=1, max_age=90, now_monotonic=105, current_boot_id=boot)
        self.assertTrue(value["status_fresh"])
        self.assertEqual(value["status_age_seconds"], 5)
        self.assertEqual(value["freshness_clock"], "monotonic")

    def test_different_boot_or_future_sample_is_not_fresh(self):
        raw = cached()
        raw["boot_id"] = "00000000-0000-0000-0000-000000000001"
        value = metrics.normalize_status(raw, now=1001, max_age=90, now_monotonic=105,
                                          current_boot_id="00000000-0000-0000-0000-000000000002")
        self.assertFalse(value["status_fresh"])
        self.assertFalse(metrics.normalize_status(cached(), now=999, max_age=90)["status_fresh"])
        raw["boot_id"] = "00000000-0000-0000-0000-000000000001"
        self.assertFalse(metrics.normalize_status(raw, now=1005, max_age=90, now_monotonic=99,
                                                  current_boot_id=raw["boot_id"])["status_fresh"])

    def test_same_boot_missing_monotonic_cannot_fall_back_to_fresh_wall_clock(self):
        raw = cached()
        raw["boot_id"] = "00000000-0000-0000-0000-000000000001"
        del raw["sample_monotonic"]
        with self.assertRaises(metrics.StatusError):
            metrics.normalize_status(raw, now=1005, max_age=90, now_monotonic=105, current_boot_id=raw["boot_id"])

    def test_production_adapter_and_legacy_readings_do_not_fabricate_proof(self):
        raw = {"utc": "1970-01-01T00:16:40+00:00", "monotonic": 100, "requested_floor_pct": 55,
               "control_verified": True, "control_path": "private-address", "fault_latched": False,
               "gpus": [{"gpu": 1, "core_c": 42, "hbm_c": 43, "power_w": 25, "util_pct": 0}],
               "chassis_readback": {"utc": "1970-01-01T00:16:38+00:00", "fans": [{"fan": 1, "rpm": 10000, "actual_pct": 55}]}}
        value = metrics.normalize_status(raw, now=1005, max_age=90)
        self.assertEqual(value["source"], "production-v1")
        self.assertEqual(value["gpus"][0]["memory_c"], 43)
        self.assertEqual(value["fans"][0]["rpm"], 10000)
        self.assertIsNone(value["fan_rpm_verified"])
        self.assertFalse(value["physical_fresh"])

    def test_production_new_diagnostics_separate_stale_setting_proof(self):
        raw = {"utc": "1970-01-01T00:16:40+00:00", "monotonic": 100, "requested_floor_pct": 55,
               "control_verified": True, "gpus": [], "cooling_diagnostics": {"schema": 1,
               "setting_proof": {"verified": True, "fresh": False}, "physical_diagnostics": cached()["physical_diagnostics"]}}
        value = metrics.normalize_status(raw, now=1005, max_age=90)
        self.assertFalse(value["floor_setting_verified"])
        self.assertTrue(value["physical_fresh"])

    def test_setting_proof_also_expires_at_its_source_ttl(self):
        raw = {"utc": "1970-01-01T00:16:40+00:00", "monotonic": 100, "requested_floor_pct": 55,
               "control_verified": True, "gpus": [], "cooling_diagnostics": {"schema": 1,
               "setting_proof": {"verified": True, "fresh": True, "age_seconds": 89, "max_age_seconds": 90}}}
        value = metrics.normalize_status(raw, now=1005, max_age=900)
        self.assertTrue(value["status_fresh"])
        self.assertEqual(value["floor_setting_age_seconds"], 94)
        self.assertFalse(value["floor_setting_verified"])

    def test_nonfinite_numeric_injection_or_unknown_sensor_names_are_omitted(self):
        raw = cached()
        raw["gpus"][0]["power_w"] = float("nan")
        raw["gpus"].append({"index_in_sample": '1"} private', "core_c": 42})
        raw["physical_diagnostics"]["temperatures"].append({"kind": "private-host", "index": 1, "temperature_c": 42})
        rendered = metrics.prometheus(metrics.normalize_status(raw, now=1005, max_age=90))
        self.assertNotIn("nan", rendered.lower())
        self.assertNotIn("private", rendered)
        self.assertNotIn("gpu_power_w", rendered)
        raw["gpus"][0]["power_w"] = 10 ** 1000
        self.assertIsNone(metrics.normalize_status(raw, now=1005, max_age=90)["gpus"][0]["power_w"])

    def test_cached_cli_does_not_import_or_call_any_hardware_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            path.write_text(json.dumps(cached()))
            output = io.StringIO()
            with patch("subprocess.run", side_effect=AssertionError("no subprocess allowed")), \
                 patch("socket.socket", side_effect=AssertionError("no sockets allowed")), \
                 patch("status_metrics.time.time", return_value=1005), contextlib.redirect_stdout(output):
                code = metrics.main(["--status-file", str(path)])
            self.assertEqual(code, 0)
            self.assertIn("nvidia_idrac_status_available 1", output.getvalue())

    def test_invalid_or_unavailable_status_is_sanitized_and_exits_nonzero(self):
        for raw in ({}, {"schema": 2}, {"schema": 1, "time_unix": float("nan")}):
            with self.assertRaises(metrics.StatusError):
                metrics.normalize_status(raw, now=1005, max_age=90)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(metrics.main(["--status-file", "/nonexistent/private/path"]), 2)
        self.assertNotIn("private", output.getvalue())

    def test_ambiguous_or_non_regular_status_is_refused_without_blocking(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            path.write_text('{"schema":1,"schema":2}')
            with self.assertRaises(metrics.StatusError):
                metrics.read_status(path)
            path.unlink()
            os.mkfifo(path)
            with self.assertRaises(metrics.StatusError):
                metrics.read_status(path)
            path.unlink()
            path.symlink_to(Path(tmp) / "missing-private-file")
            with self.assertRaises(metrics.StatusError):
                metrics.read_status(path)


if __name__ == "__main__":
    unittest.main()
