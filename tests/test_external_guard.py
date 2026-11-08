"""Offline external-guard tests: fake SSH, synthetic clocks, temporary files only."""
import contextlib
import dataclasses
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import external_guard as guard

BOOT = "11111111-2222-3333-4444-555555555555"
OTHER_BOOT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def config(root=Path("/example")):
    return guard.Config("guest.example.net", "heartbeat", root / "identity", root / "known_hosts",
                        root / "state.json", root / "status.json", root / "lock")


def source(**changes):
    return {"schema": 1, "boot_id": BOOT, "sample_monotonic": 90.0, "cycle_sequence": 1,
            "controller_running": True, "dry_run": False, "fault": False,
            "floor_setting_verified": True, "control_verified": True,
            "verified_floor_pct": 55, **changes}


def sample(**changes):
    return {"protocol": 1, "guest_boot_id": BOOT, "sample_monotonic": 90.0,
            "helper_monotonic": 100.0, "sample_age_seconds": 10.0, "cycle_sequence": 1,
            "controller_running": True, "dry_run": False, "fault": False,
            "floor_setting_verified": True, "verified_floor_pct": 55, **changes}


def production(**changes):
    return {"utc": "2026-01-01T00:00:00+00:00", "boot_id": BOOT, "monotonic": 90.0,
            "cycle_sequence": 1, "controller_running": True, "invocation_id": "synthetic-invocation-1",
            "requested_floor_pct": 55, "control_verified": True, "fault_latched": False,
            "control_degraded": False, "gpus": [{"gpu": 0, "core_c": 40, "hbm_c": 45,
                                                   "power_w": 25, "util_pct": 0}], **changes}


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "status.json"

    def read(self, value=None, **options):
        self.path.write_text(json.dumps(source() if value is None else value))
        self.path.chmod(0o600)
        return guard.heartbeat(self.path, current_boot=BOOT, now=100.0, **options)

    def test_age_is_guest_monotonic_not_cross_host_wall_clock(self):
        for stamp in [0, 1e30, -1e30]:
            result = self.read(source(time_unix=stamp))
            self.assertEqual(result["sample_age_seconds"], 10.0)

    def test_prior_boot_and_future_samples_rejected(self):
        for changes in [{"boot_id": OTHER_BOOT}, {"sample_monotonic": 101}, {"sample_monotonic": -1},
                        {"sample_monotonic": True}, {"cycle_sequence": 0}, {"cycle_sequence": True}]:
            with self.subTest(changes=changes), self.assertRaises(guard.GuardError):
                self.read(source(**changes))

    def test_missing_boot_or_monotonic_is_incompatible_not_healthy(self):
        for field in ["boot_id", "sample_monotonic", "cycle_sequence", "floor_setting_verified"]:
            value = source()
            value.pop(field)
            with self.subTest(field=field), self.assertRaises(guard.GuardError):
                self.read(value)

    def test_typed_safety_and_verification_fields(self):
        for field in ["controller_running", "dry_run", "fault", "floor_setting_verified"]:
            with self.subTest(field=field), self.assertRaises(guard.GuardError):
                self.read(source(**{field: 1}))
        with self.assertRaises(guard.GuardError):
            self.read(source(control_verified=False))
        with self.assertRaises(guard.GuardError):
            self.read(source(verified_floor_pct=True))

    def test_guest_helper_keeps_its_linux_monotonic_clock(self):
        self.path.write_text(json.dumps(source()))
        self.path.chmod(0o600)
        with patch.object(guard.time, "monotonic", return_value=100), \
             patch.object(guard.time, "clock_gettime", side_effect=AssertionError("management clock used by guest")):
            result = guard.heartbeat(self.path, current_boot=BOOT)
        self.assertEqual(result["sample_age_seconds"], 10)

    def test_response_omits_raw_telemetry_and_identifiers(self):
        result = self.read(source(gpus=[{"identity": "GPU-synthetic-private", "core_c": 40}], reason="private diagnostic"))
        text = json.dumps(result)
        for word in ["GPU-synthetic-private", "private diagnostic", "core_c", "gpus"]:
            self.assertNotIn(word, text)

    def test_private_regular_file_no_symlinks_or_untrusted_writes(self):
        self.read()
        self.path.chmod(0o666)
        with self.assertRaises(guard.GuardError):
            guard.heartbeat(self.path, current_boot=BOOT, now=100)
        self.path.chmod(0o600)
        alias = self.path.parent / "alias"
        alias.symlink_to(self.path)
        with self.assertRaises(guard.GuardError):
            guard.heartbeat(alias, current_boot=BOOT, now=100)

    def test_oversized_or_ambiguous_json_rejected(self):
        for text in ['{"schema":1,"schema":1}', '{"sample":NaN}', 'x' * (guard.MAX_JSON_BYTES + 1)]:
            self.path.write_text(text)
            self.path.chmod(0o600)
            with self.subTest(size=len(text)), self.assertRaises(guard.GuardError):
                guard.heartbeat(self.path, current_boot=BOOT, now=100)

    def test_production_is_explicit_typed_and_omits_private_fields(self):
        result = self.read(production(control_path="private-endpoint", reason="private diagnostic"), status_format="production")
        self.assertEqual(result["sample_age_seconds"], 10)
        self.assertFalse(result["control_degraded"])
        self.assertEqual(result["verified_floor_pct"], 55)
        for private in ["private-endpoint", "private diagnostic", "synthetic-invocation-1", "gpus"]:
            self.assertNotIn(private, json.dumps(result))
        with self.assertRaises(guard.GuardError):
            self.read(production())  # No auto-detection of a different schema.

    def test_production_stopped_and_degraded_never_look_healthy(self):
        for changes, reason in [({"controller_running": False, "gpus": []}, "controller-stopped"),
                                ({"control_degraded": True, "requested_floor_pct": 100}, "control-degraded")]:
            result = self.read(production(**changes), status_format="production")
            observed = guard.Monitor(config(), guard.State(BOOT)).observe(result, 0)
            self.assertTrue(observed["alert"])
            self.assertEqual(observed["reason"], reason)

    def test_production_freshness_and_required_safety_fields(self):
        for name in ["controller_running", "invocation_id", "cycle_sequence", "monotonic", "boot_id",
                     "control_degraded", "fault_latched", "control_verified", "utc", "gpus"]:
            value = production()
            value.pop(name)
            with self.subTest(name=name), self.assertRaises(guard.GuardError):
                self.read(value, status_format="production")
        for changes in [{"controller_running": 1}, {"control_degraded": 0}, {"requested_floor_pct": True},
                        {"monotonic": 101}, {"gpus": []}, {"utc": "2026-01-01"},
                        {"gpus": [{"gpu": 0, "core_c": "40", "hbm_c": 45, "power_w": 25, "util_pct": 0}]}]:
            with self.subTest(changes=changes), self.assertRaises(guard.GuardError):
                self.read(production(**changes), status_format="production")


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.m = guard.Monitor(config(), guard.State(BOOT))

    def test_unreachable_requires_elapsed_qualification(self):
        self.assertEqual(self.m.observe(None, 0, error=True)["phase"], "qualifying")
        self.assertEqual(self.m.observe(None, 29, error=True)["phase"], "qualifying")
        self.assertEqual(self.m.observe(None, 30, error=True)["phase"], "alert")

    def test_stale_requires_qualification_and_no_bmc_write_exists(self):
        stale = sample(helper_monotonic=221, sample_age_seconds=131)
        self.assertEqual(self.m.observe(stale, 0)["reason"], "heartbeat-stale")
        result = self.m.observe(stale, 30)
        self.assertTrue(result["alert"])
        self.assertFalse(result["bmc_write_attempted"])
        self.assertFalse(result["hardware_cooling_verified"])

    def test_fault_stop_dry_run_or_unverified_alert_immediately(self):
        for change in [{"fault": True}, {"controller_running": False}, {"dry_run": True},
                       {"floor_setting_verified": False, "verified_floor_pct": None}]:
            m = guard.Monitor(config(), guard.State(BOOT))
            with self.subTest(change=change):
                self.assertTrue(m.observe(sample(**change), 0)["alert"])

    def test_transient_outage_resets_qualification(self):
        self.m.observe(None, 0, error=True)
        self.assertEqual(self.m.observe(sample(), 20)["phase"], "healthy")
        self.assertEqual(self.m.observe(None, 30, error=True)["phase"], "qualifying")
        self.assertEqual(self.m.observe(None, 59, error=True)["phase"], "qualifying")

    def test_recovery_needs_distinct_successful_cycles(self):
        self.m.observe(None, 0, error=True)
        self.m.observe(None, 30, error=True)
        self.assertEqual(self.m.observe(sample(), 40)["phase"], "recovering")
        for at in [50, 60, 70]:
            self.assertEqual(self.m.observe(sample(), at)["phase"], "recovering")
        self.m.observe(sample(sample_monotonic=91, sample_age_seconds=9, cycle_sequence=2), 80)
        result = self.m.observe(sample(sample_monotonic=92, sample_age_seconds=8, cycle_sequence=3), 90)
        self.assertEqual(result["phase"], "healthy")
        self.assertFalse(result["alert"])

    def test_bad_recovery_sample_resets_good_counter(self):
        self.m.observe(sample(fault=True), 0)
        self.m.observe(sample(), 1)
        self.m.observe(None, 2, error=True)
        self.assertEqual(self.m.state.recovery_count, 0)
        self.assertTrue(self.m.state.alert)

    def test_reordered_cycles_cannot_clear_alert_or_lower_watermark(self):
        self.m.observe(sample(fault=True), 0)
        self.m.observe(sample(sample_monotonic=91, sample_age_seconds=9, cycle_sequence=2), 1)
        self.assertEqual(self.m.state.recovery_count, 1)
        regressed = self.m.observe(sample(), 2)
        self.assertEqual(regressed["reason"], "heartbeat-not-advancing")
        repeated = self.m.observe(sample(sample_monotonic=91, sample_age_seconds=9, cycle_sequence=2), 3)
        self.assertTrue(repeated["alert"])
        self.assertEqual(repeated["recovery_samples"], 0)
        self.assertEqual(self.m.state.cycle_sequence, 2)
        self.assertEqual(self.m.state.sample_monotonic, 91)
        for sequence in [3, 4, 5]:
            result = self.m.observe(sample(sample_monotonic=89 + sequence, sample_age_seconds=11 - sequence,
                                           cycle_sequence=sequence), sequence + 1)
        self.assertFalse(result["alert"])

    def test_same_sequence_with_new_timestamp_cannot_count_as_recovery(self):
        self.m.observe(sample(fault=True), 0)
        for at in [1, 2, 3]:
            result = self.m.observe(sample(sample_monotonic=90 + at, sample_age_seconds=10 - at), at)
            self.assertEqual(result["reason"], "heartbeat-not-advancing")
            self.assertTrue(result["alert"])
            self.assertEqual(result["recovery_samples"], 0)

    def test_producer_or_boot_change_resets_partial_recovery(self):
        for change in [{"producer_instance": "a" * 64}, {"guest_boot_id": OTHER_BOOT}]:
            with self.subTest(change=change):
                m = guard.Monitor(config(), guard.State(BOOT))
                m.observe(sample(fault=True), 0)
                m.observe(sample(sample_monotonic=91, sample_age_seconds=9, cycle_sequence=2), 1)
                m.observe(sample(sample_monotonic=92, sample_age_seconds=8, cycle_sequence=3), 2)
                result = m.observe(sample(**change), 3)
                self.assertTrue(result["alert"])
                self.assertEqual(result["recovery_samples"], 1)

    def test_receipts_have_no_boot_fingerprints_or_endpoint(self):
        result = json.dumps(self.m.observe(sample(), 0))
        self.assertNotIn(BOOT, result)
        self.assertNotIn("guest.example.net", result)
        self.assertNotIn("heartbeat@", result)

    def test_malformed_protocol_is_not_a_healthy_sample(self):
        for value in [sample(protocol=True), sample(fault=0), sample(sample_age_seconds=0),
                      sample(sample_monotonic=101), sample(guest_boot_id="bad")]:
            with self.subTest(value=value), self.assertRaises(guard.GuardError):
                guard.validate_heartbeat(value)


class StateTests(unittest.TestCase):
    def test_legacy_or_other_clock_domain_preserves_alert_but_resets_timing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            for qualified in [False, True]:
                for domain in [None, "process-relative-legacy"]:
                    with self.subTest(qualified=qualified, domain=domain):
                        raw = dataclasses.asdict(guard.State(BOOT, bad_since=0.046, alert=qualified,
                                                           recovery_count=2, last_observation=0.056))
                        if domain is None:
                            raw.pop("clock_domain")
                        else:
                            raw["clock_domain"] = domain
                        guard.write_private(path, raw)
                        restored = guard.load_state(path, BOOT)
                        self.assertEqual(restored.alert, qualified)
                        self.assertEqual(restored.clock_domain, guard.CLOCK_DOMAIN)
                        self.assertIsNone(restored.bad_since)
                        self.assertIsNone(restored.last_observation)
                        self.assertEqual(restored.recovery_count, 0)
                        self.assertIsNone(restored.guest_boot_id)

    def test_restart_preserves_qualified_alert_and_same_boot_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = guard.State(BOOT, bad_since=10, alert=True, recovery_count=1, last_observation=40)
            guard.write_private(path, dataclasses.asdict(state))
            restored = guard.load_state(path, BOOT)
            self.assertTrue(restored.alert)
            self.assertEqual(restored.bad_since, 10)
            rebooted = guard.load_state(path, OTHER_BOOT)
            self.assertTrue(rebooted.alert)
            self.assertIsNone(rebooted.bad_since)
            self.assertEqual(rebooted.recovery_count, 0)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_state_is_not_reset_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text('{"alert":false}')
            with self.assertRaises(guard.GuardError):
                guard.load_state(path, BOOT)

    def test_restart_retains_advancing_watermark_and_rejects_partial_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            m = guard.Monitor(config(), guard.State(BOOT))
            m.observe(sample(fault=True), 0)
            m.observe(sample(sample_monotonic=91, sample_age_seconds=9, cycle_sequence=2), 1)
            guard.write_private(path, dataclasses.asdict(m.state))
            restored = guard.load_state(path, BOOT)
            self.assertEqual(restored.recovery_count, 1)
            self.assertEqual(restored.cycle_sequence, 2)
            result = guard.Monitor(config(), restored).observe(sample(), 2)
            self.assertTrue(result["alert"])
            self.assertEqual(result["recovery_samples"], 0)
            invalid = dataclasses.asdict(restored)
            invalid["sample_monotonic"] = None
            guard.write_private(path, invalid)
            with self.assertRaises(guard.GuardError):
                guard.load_state(path, BOOT)


class ManagementBootTests(unittest.TestCase):
    def test_linux_uses_local_boot_file_without_sysctl(self):
        with patch.object(guard, "boot_id", return_value=BOOT) as read:
            self.assertEqual(guard.management_boot_id(system="Linux"), BOOT)
            read.assert_called_once_with()

    def test_darwin_uses_fixed_bounded_local_command_and_validates_uuid(self):
        calls = []
        def run(args, **options):
            calls.append((args, options))
            return subprocess.CompletedProcess(args, 0, BOOT.upper() + "\n", "")
        self.assertEqual(guard.management_boot_id(system="Darwin", runner=run), BOOT)
        self.assertEqual(calls[0][0], ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"])
        self.assertEqual(calls[0][1]["timeout"], 5)
        for output in ["private unexpected value", BOOT + "\n" + OTHER_BOOT]:
            with self.assertRaises(guard.GuardError) as error:
                guard.management_boot_id(system="Darwin", runner=lambda *a, **kw: subprocess.CompletedProcess([], 0, output, ""))
            self.assertNotIn("private", str(error.exception))

    def test_darwin_timeout_and_unknown_platform_fail_closed(self):
        def failed(*args, **options):
            raise subprocess.TimeoutExpired(["sysctl"], 5, output="private")
        with self.assertRaises(guard.GuardError):
            guard.management_boot_id(system="Darwin", runner=failed)
        with self.assertRaises(guard.GuardError):
            guard.management_boot_id(system="Unknown")


class ManagementClockTests(unittest.TestCase):
    def test_darwin_uses_global_clock_instead_of_python39_relative_origin(self):
        with patch.object(guard.time, "monotonic", return_value=0.007) as relative, \
             patch.object(guard.time, "clock_gettime", return_value=555926) as global_clock:
            self.assertEqual(guard.management_monotonic(system="Darwin"), 555926)
            global_clock.assert_called_once_with(guard.time.CLOCK_MONOTONIC)
            relative.assert_not_called()

    def test_linux_retains_ordinary_system_monotonic(self):
        with patch.object(guard.time, "monotonic", return_value=123) as mono, \
             patch.object(guard.time, "clock_gettime", side_effect=AssertionError("wrong adapter")):
            self.assertEqual(guard.management_monotonic(system="Linux"), 123)
            mono.assert_called_once_with()

    def test_management_clock_invalid_values_or_unavailable_fail_closed(self):
        for value in [float("nan"), float("inf"), -1, True, "555926"]:
            with self.subTest(value=value), patch.object(guard.time, "clock_gettime", return_value=value), \
                 self.assertRaises(guard.GuardError):
                guard.management_monotonic(system="Darwin")
        with patch.object(guard.time, "clock_gettime", side_effect=OSError("private details")), \
             self.assertRaises(guard.GuardError) as error:
            guard.management_monotonic(system="Darwin")
        self.assertNotIn("private", str(error.exception))
        with self.assertRaises(guard.GuardError):
            guard.management_monotonic(system="Unknown")


class ConfigAndSSHTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "config.json"
        key, trust = self.root / "identity", self.root / "known_hosts"
        key.write_text("synthetic-private-file-not-a-key")
        trust.write_text("guest.example.net ssh-ed25519 synthetic-not-a-host-key\n")
        key.chmod(0o600)
        trust.chmod(0o600)
        self.raw = dataclasses.asdict(config(self.root))
        self.raw = {k: str(v) if isinstance(v, Path) else v for k, v in self.raw.items()}

    def load(self, **changes):
        self.path.write_text(json.dumps({**self.raw, **changes}))
        self.path.chmod(0o600)
        return guard.load_config(self.path)

    def test_valid_config_and_pinned_ssh_fixed_read_only_helper(self):
        command = guard.ssh_command(self.load())
        for option in ["StrictHostKeyChecking=yes", "BatchMode=yes", "IdentitiesOnly=yes",
                       "PasswordAuthentication=no", "KbdInteractiveAuthentication=no"]:
            self.assertIn(option, command)
        self.assertEqual(command[-7:], ["/usr/bin/python3", self.raw["remote_helper"], "heartbeat", "--status-file", self.raw["remote_status_file"], "--status-format", "generic"])
        self.assertNotIn("racadm", command)
        self.assertNotIn("--apply", command)

    def test_shell_host_remote_path_and_ssh_expansion_rejected(self):
        for values in [{"guest_host": "-oProxyCommand=bad"}, {"guest_user": "bad;true"},
                       {"remote_helper": "/path;true"}, {"remote_status_file": "/a/../b"},
                       {"identity_file": self.raw["identity_file"] + "%h"},
                       {"known_hosts_file": self.raw["known_hosts_file"] + " space"}]:
            with self.subTest(values=values), self.assertRaises(guard.GuardError):
                self.load(**values)

    def test_alias_and_permissions_rejected(self):
        for field in ["status_file", "state_file", "lock_file"]:
            with self.subTest(field=field), self.assertRaises(guard.GuardError):
                self.load(**{field: self.raw["identity_file"]})
        Path(self.raw["identity_file"]).chmod(0o644)
        with self.assertRaises(guard.GuardError):
            self.load()

    def test_invalid_types_ranges_and_unknown_keys_rejected(self):
        for values in [{"ssh_port": True}, {"recovery_samples": 0}, {"interval_seconds": float("nan")},
                       {"state_file": "relative"}, {"ssh": "ssh"}, {"apply": True}, {"extra": 1},
                       {"remote_status_format": "arbitrary-command"}]:
            with self.subTest(values=values), self.assertRaises(guard.GuardError):
                self.load(**values)

    def test_timeout_nonzero_and_raw_output_are_sanitized(self):
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(["ssh", "private"], 1, output="private")
        for runner in [timeout, lambda *a, **kw: subprocess.CompletedProcess([], 1, "private", "private")]:
            with self.assertRaises(guard.HeartbeatError) as error:
                guard.fetch_heartbeat(self.load(), runner=runner)
            self.assertNotIn("private", str(error.exception))
        result = guard.fetch_heartbeat(self.load(), runner=lambda *a, **kw: subprocess.CompletedProcess([], 0, json.dumps(sample()), ""))
        self.assertEqual(result["cycle_sequence"], 1)


class CLITests(unittest.TestCase):
    def test_mac39_cross_process_qualification_uses_global_persisted_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = config(Path(tmp))
            # Each invocation's Python 3.9 relative clock would start near zero,
            # including a backwards jump. The system clock advances 31 seconds.
            with patch.object(guard, "load_config", return_value=c), patch.object(guard, "management_boot_id", return_value=BOOT), \
                 patch.object(guard.platform, "system", return_value="Darwin"), \
                 patch.object(guard.time, "monotonic", side_effect=[0.046, 0.031]) as relative, \
                 patch.object(guard.time, "clock_gettime", side_effect=[555926, 555957]) as global_clock, \
                 patch.object(guard, "fetch_heartbeat", side_effect=guard.HeartbeatError("synthetic unavailable")), \
                 patch.object(guard.signal, "signal"), patch.object(guard, "notify"), \
                 patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()):
                first = guard.main(["monitor", "--config", "/synthetic/config.json", "--once"])
                second = guard.main(["monitor", "--config", "/synthetic/config.json", "--once"])
            self.assertEqual((first, second), (2, 1))
            relative.assert_not_called()
            self.assertEqual(global_clock.call_count, 2)
            state = json.loads(c.state_file.read_text())
            self.assertEqual(state["clock_domain"], guard.CLOCK_DOMAIN)
            self.assertEqual(state["bad_since"], 555926)
            self.assertEqual(state["last_observation"], 555957)
            self.assertTrue(state["alert"])

    def test_persistent_stdout_only_logs_initial_and_state_transitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = config(Path(tmp))
            class StopAfterThree:
                calls = 0
                def set(self):
                    pass
                def wait(self, _seconds):
                    self.calls += 1
                    return self.calls == 3
            output = io.StringIO()
            with patch.object(guard, "load_config", return_value=c), patch.object(guard, "management_boot_id", return_value=BOOT), \
                 patch.object(guard, "fetch_heartbeat", return_value=sample()), \
                 patch.object(guard.threading, "Event", return_value=StopAfterThree()), \
                 patch.object(guard.signal, "signal"), patch.object(guard, "notify"), \
                 patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(output):
                result = guard.main(["monitor", "--config", "/synthetic/config.json"])
            self.assertEqual(result, 0)
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(len(events), 2)
            self.assertTrue(events[0]["monitor_running"])
            self.assertFalse(events[-1]["monitor_running"])
            self.assertEqual(json.loads(c.state_file.read_text())["cycle_sequence"], 1)

    def test_once_exit_codes_and_no_writes_outside_private_receipts(self):
        for value, expected in [(sample(), 0), (sample(fault=True), 1), (None, 2)]:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                c = config(Path(tmp))
                effect = guard.HeartbeatError("synthetic unavailable") if value is None else None
                with patch.object(guard, "load_config", return_value=c), patch.object(guard, "management_boot_id", return_value=BOOT), \
                     patch.object(guard, "fetch_heartbeat", return_value=value, side_effect=effect), \
                     patch.object(guard.signal, "signal"), patch.object(guard, "notify"), \
                     patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()):
                    result = guard.main(["monitor", "--config", "/synthetic/config.json", "--once"])
                self.assertEqual(result, expected)
                status = json.loads(c.status_file.read_text())
                self.assertFalse(status["monitor_running"])
                self.assertFalse(status["bmc_write_attempted"])

    def test_apply_is_not_an_available_mode(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
            guard.main(["monitor", "--config", "/synthetic/config.json", "--apply"])
        self.assertEqual(result.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
