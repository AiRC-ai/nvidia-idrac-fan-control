"""Synthetic/offline tests. No SSH, GPU, or fan writes occur."""
import dataclasses
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import nvidia_idrac_fan as fan


def gpu(identity="GPU-synthetic-a", core=40, memory=45, watts=25, util=0):
    return fan.GPU(identity, core, memory, watts, util)


def xml(count=2, memory="45 C", power_group="power_readings", power_field="power_draw"):
    return '<nvidia_smi_log>' + ''.join(
        f'<gpu><uuid>GPU-synthetic-{i}</uuid><temperature><gpu_temp>40 C</gpu_temp>'
        f'<memory_temp>{memory}</memory_temp></temperature><utilization><gpu_util>0 %</gpu_util></utilization>'
        f'<{power_group}><{power_field}>25.5 W</{power_field}></{power_group}></gpu>' for i in range(count)
    ) + '</nvidia_smi_log>'


THERMAL = '''[Key=System.Embedded.1#ThermalSettings.1]
MinimumFanSpeed=55
#MFSMinimumLimit=0
#MFSMaximumLimit=100
ThirdPartyPCIFanResponse=1
ThermalProfile=0
'''


def config():
    return fan.Config('idrac.example.net', 'fancontrol', Path('/example/id_ed25519'),
                      Path('/example/known_hosts'), 2, True, Path('/example/status.json'),
                      Path('/example/controller.lock'), fan.PolicyConfig())


class ParserTests(unittest.TestCase):
    def test_nvidia_two_gpus(self):
        values = fan.parse_nvidia_xml(xml(), 2, True)
        self.assertEqual(len(values), 2)
        self.assertEqual(values[1].core_c, 40)
        self.assertEqual(values[1].memory_c, 45)
        self.assertEqual(values[1].power_w, 25.5)

    def test_nvidia_new_power_and_memory_tags(self):
        text = xml(power_group='gpu_power_readings', power_field='instant_power_draw').replace('memory_temp', 'gpu_memory_temp')
        self.assertEqual(fan.parse_nvidia_xml(text, 2, True)[0].memory_c, 45)

    def test_memory_optional_only_when_explicit(self):
        self.assertIsNone(fan.parse_nvidia_xml(xml(memory='N/A'), 2, False)[0].memory_c)
        with self.assertRaises(fan.TelemetryError):
            fan.parse_nvidia_xml(xml(memory='N/A'), 2, True)

    def test_malformed_optional_memory_is_not_ignored(self):
        for value in ['bad', 'NaN C', '-1 C', '141 C', '1e2 C']:
            with self.subTest(value=value), self.assertRaises(fan.TelemetryError):
                fan.parse_nvidia_xml(xml(memory=value), 2, False)

    def test_missing_gpu_or_duplicate_identity(self):
        for value in [xml(count=1), xml().replace('GPU-synthetic-1', 'GPU-synthetic-0')]:
            with self.assertRaises(fan.TelemetryError):
                fan.parse_nvidia_xml(value, 2, True)

    def test_invalid_or_entity_xml(self):
        for value in ['<bad', '<!DOCTYPE a [<!ENTITY x "test">]>' + xml(), '<other></other>']:
            with self.assertRaises(fan.TelemetryError):
                fan.parse_nvidia_xml(value, 2, True)

    def test_missing_activity_fails_safe(self):
        for value in [xml().replace('25.5 W', 'N/A'), xml().replace('0 %', 'N/A')]:
            with self.assertRaises(fan.TelemetryError):
                fan.parse_nvidia_xml(value, 2, True)

    def test_thermal_limits(self):
        self.assertEqual(fan.parse_thermal_settings(THERMAL), fan.ThermalSettings(55, 0, 100, 1))
        self.assertEqual(fan.parse_thermal_settings(THERMAL.replace('MinimumFanSpeed=55', 'MinimumFanSpeed=255')).minimum_pct, 255)
        self.assertEqual(fan.parse_thermal_settings(THERMAL.replace('FanResponse=1', 'FanResponse=Enabled')).pci_response, 1)

    def test_thermal_unsupported_or_unsafe(self):
        for value in [THERMAL.replace('FanResponse=1', 'FanResponse=0'),
                      THERMAL.replace('#MFSMaximumLimit=100', '#MFSMaximumLimit=84'),
                      THERMAL.replace('ThirdPartyPCIFanResponse=1', ''),
                      THERMAL + 'MinimumFanSpeed=55\n',
                      THERMAL.replace('MinimumFanSpeed=55', 'MinimumFanSpeed=bad')]:
            with self.subTest(value=value), self.assertRaises(fan.ControlError):
                fan.parse_thermal_settings(value)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.p = fan.Policy(fan.PolicyConfig())

    def test_startup_and_stepwise_idle_cooldown(self):
        self.assertEqual(self.p.choose([gpu()], 0)[0], 100)
        self.assertEqual(self.p.choose([gpu()], 59)[0], 100)
        self.assertEqual(self.p.choose([gpu()], 60)[0], 95)
        self.assertEqual(self.p.choose([gpu()], 120)[0], 90)

    def test_active_floors_respond_to_use_not_allocated_memory(self):
        self.p.current = 55
        self.assertEqual(self.p.choose([gpu(watts=120)], 0), (84, 'active'))
        self.p.current = 55
        self.assertEqual(self.p.choose([gpu(util=80)], 1)[0], 84)
        self.p.current = 55
        self.assertEqual(self.p.choose([gpu(watts=25, util=0)], 2)[0], 55)

    def test_both_gpus_and_memory_drive_demand(self):
        self.p.current = 55
        self.assertEqual(self.p.choose([gpu(core=75), gpu('GPU-synthetic-b', core=35)], 0)[0], 100)
        self.p.current = 55
        self.assertEqual(self.p.choose([gpu(memory=80)], 1)[0], 100)

    def test_critical_is_reported_without_stopping_workloads(self):
        self.assertEqual(self.p.choose([gpu(memory=90)], 0), (100, 'critical-temperature-no-workload-stop'))

    def test_dynamic_idle_increase(self):
        self.p.current = 55
        self.assertEqual(self.p.choose([gpu(core=58)], 0), (75, 'idle-thermal-demand'))

    def test_hysteresis_breaks_cooldown(self):
        self.p.current = 84
        self.p.choose([gpu()], 0)
        self.p.choose([gpu(watts=100)], 59)
        self.assertEqual(self.p.choose([gpu()], 60)[0], 84)
        self.assertEqual(self.p.choose([gpu()], 120)[0], 79)

    def test_fault_resets_cooldown(self):
        self.p.choose([gpu()], 0)
        self.p.fault()
        self.assertEqual(self.p.choose([gpu()], 61)[0], 100)
        self.assertEqual(self.p.choose([gpu()], 121)[0], 95)

    def test_bad_policy_rejected(self):
        for values in [dict(core_target_c=90), dict(cooldown_seconds=0), dict(gain_pct_per_c=float('nan')),
                       dict(idle_floor_pct=55.5), dict(active_floor_pct=101), dict(idle_floor_pct=True)]:
            with self.subTest(values=values), self.assertRaises(fan.ConfigError):
                fan.PolicyConfig(**values).validate()


class FakeIDRAC:
    def __init__(self):
        self.floor = 55
        self.writes = []
        self.fail = False

    def probe(self):
        if self.fail:
            raise fan.ControlError('synthetic connection loss')
        return fan.ThermalSettings(self.floor, 0, 100, 1)

    def set_floor(self, value):
        self.writes.append(value)
        if self.fail:
            raise fan.ControlError('synthetic write loss')
        self.floor = value
        return self.probe()


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.samples = [gpu(), gpu('GPU-synthetic-b')]
        self.idrac = FakeIDRAC()
        self.c = fan.Controller(config(), self.idrac, apply=True, telemetry=lambda: self.samples)

    def test_success_is_readback_verified(self):
        result = self.c.step(0)
        self.assertEqual(self.idrac.writes, [100])
        self.assertTrue(result['control_verified'])
        self.assertFalse(result['fault'])

    def test_control_loss_retries_100_every_cycle(self):
        self.idrac.fail = True
        for now in [0, 5, 10]:
            result = self.c.step(now)
            self.assertTrue(result['fault'])
            self.assertFalse(result['control_verified'])
            self.assertIsNone(result['verified_floor_pct'])
        self.assertEqual(self.idrac.writes, [100, 100, 100])

    def test_control_recovery_remains_high_until_cooldown(self):
        self.idrac.fail = True
        self.c.step(0)
        self.idrac.fail = False
        self.assertEqual(self.c.step(5)['verified_floor_pct'], 100)
        self.assertEqual(self.c.step(65)['verified_floor_pct'], 95)

    def test_telemetry_loss_raises_floor(self):
        def failed():
            raise fan.TelemetryError('synthetic unavailable')
        self.c.telemetry = failed
        result = self.c.step(0)
        self.assertTrue(result['fault'])
        self.assertEqual(result['verified_floor_pct'], 100)

    def test_critical_temperature_is_a_fault_with_verified_maximum(self):
        self.samples[0] = gpu(memory=90)
        result = self.c.step(0)
        self.assertTrue(result['fault'])
        self.assertEqual(result['reason'], 'critical-temperature-no-workload-stop')
        self.assertEqual(result['verified_floor_pct'], 100)

    def test_inventory_change_fails_safe(self):
        self.c.step(0)
        self.samples[1] = gpu('GPU-replacement')
        self.assertTrue(self.c.step(1)['fault'])

    def test_memory_disappearing_fails_safe_even_when_optional(self):
        self.c.config = dataclasses.replace(self.c.config, require_memory_temperature=False)
        self.c.step(0)
        self.samples[1] = gpu('GPU-synthetic-b', memory=None)
        self.assertTrue(self.c.step(1)['fault'])

    def test_dry_run_never_writes(self):
        self.c.apply = False
        self.c.step(0)
        self.idrac.fail = True
        self.c.step(5)
        self.assertEqual(self.idrac.writes, [])


class CommandTests(unittest.TestCase):
    def test_ssh_strict_options_and_only_minimum_property_written(self):
        calls = []
        def run(args, _timeout):
            calls.append(args)
            return 'Object value modified successfully.\n' if 'set' in args else THERMAL
        idrac = fan.IDRAC(config(), runner=run)
        idrac.set_floor(55)
        command = calls[0]
        for required in ['StrictHostKeyChecking=yes', 'BatchMode=yes', 'IdentitiesOnly=yes',
                         'PasswordAuthentication=no', 'KbdInteractiveAuthentication=no']:
            self.assertIn(required, command)
        self.assertEqual(command[-4:], ['racadm', 'set', 'System.ThermalSettings.MinimumFanSpeed', '55'])
        self.assertNotIn('shell=True', command)

    def test_missing_ack_and_wrong_readback_rejected(self):
        for reply, readback in [('unrecognized success text', THERMAL),
                                ('Object value modified successfully.', THERMAL.replace('MinimumFanSpeed=55', 'MinimumFanSpeed=60'))]:
            with self.subTest(reply=reply), self.assertRaises(fan.ControlError):
                fan.IDRAC(config(), runner=lambda args, _: reply if 'set' in args else readback).set_floor(55)

    def test_subprocess_timeout_and_failure_sanitized(self):
        for outcome in [subprocess.CompletedProcess(['ssh'], 1, '', 'private diagnostic'),
                        subprocess.TimeoutExpired(['ssh'], 1)]:
            with self.subTest(outcome=type(outcome).__name__):
                with patch('nvidia_idrac_fan.subprocess.run', side_effect=outcome if isinstance(outcome, Exception) else None,
                           return_value=outcome):
                    with self.assertRaises(fan.ControlError) as caught:
                        fan.run_command(['ssh'], 1)
                self.assertNotIn('private diagnostic', str(caught.exception))

    def test_atomic_private_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'status.json'
            fan.write_status(path, {'fault': False})
            self.assertEqual(json.loads(path.read_text()), {'fault': False})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(list(path.parent.iterdir())), 1)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        key = self.root / 'identity'
        key.write_text('synthetic-private-file-not-a-key')
        key.chmod(0o600)
        known = self.root / 'known_hosts'
        known.write_text('idrac.example.net ssh-ed25519 synthetic-not-a-host-key\n')
        known.chmod(0o600)
        self.raw = {'host': 'idrac.example.net', 'user': 'fancontrol', 'identity_file': str(key),
                    'known_hosts_file': str(known), 'status_file': str(self.root / 'status.json'),
                    'lock_file': str(self.root / 'lock'), 'expected_gpu_count': 2,
                    'require_memory_temperature': True}

    def load(self, **changes):
        path = self.root / 'config.json'
        path.write_text(json.dumps({**self.raw, **changes}))
        return fan.load_config(path)

    def test_valid_configuration(self):
        self.assertEqual(self.load().expected_gpu_count, 2)

    def test_command_injection_host_and_user_rejected(self):
        for values in [{'host': '-oProxyCommand=bad'}, {'host': 'idrac.example.net;echo bad'},
                       {'user': 'name;echo bad'}, {'host': 1}, {'user': ['bad']}]:
            with self.subTest(values=values), self.assertRaises(fan.ConfigError):
                self.load(**values)

    def test_private_key_and_host_trust_permissions(self):
        Path(self.raw['identity_file']).chmod(0o644)
        with self.assertRaises(fan.ConfigError):
            self.load()
        Path(self.raw['identity_file']).chmod(0o600)
        Path(self.raw['known_hosts_file']).chmod(0o666)
        with self.assertRaises(fan.ConfigError):
            self.load()

    def test_type_range_unknown_and_relative_path_rejected(self):
        for values in [{'expected_gpu_count': True}, {'expected_gpu_count': 0},
                       {'require_memory_temperature': 'false'}, {'interval_seconds': float('inf')},
                       {'status_file': 'relative.json'}, {'ssh': 'ssh'}, {'extra': 'unknown'}]:
            with self.subTest(values=values), self.assertRaises(fan.ConfigError):
                self.load(**values)

    def test_status_cannot_overwrite_config_lock_or_ssh_files(self):
        for name in ['identity_file', 'known_hosts_file', 'lock_file']:
            with self.subTest(name=name), self.assertRaises(fan.ConfigError):
                self.load(status_file=self.raw[name])
        with self.assertRaises(fan.ConfigError):
            self.load(status_file=str(self.root / 'config.json'))

    def test_hardlink_and_symlink_aliases_rejected(self):
        alias = self.root / 'alias'
        alias.symlink_to(Path(self.raw['known_hosts_file']))
        with self.assertRaises(fan.ConfigError):
            self.load(status_file=str(alias))
        alias.unlink()
        import os
        os.link(self.raw['known_hosts_file'], alias)
        with self.assertRaises(fan.ConfigError):
            self.load(status_file=str(alias))

    def test_ssh_filename_expansion_and_list_metacharacters_rejected(self):
        for name in ['identity_file', 'known_hosts_file']:
            for suffix in [' space', '%h', '$HOME', '"quoted"', "'quoted'", '\\backslash']:
                with self.subTest(name=name, suffix=suffix), self.assertRaises(fan.ConfigError):
                    self.load(**{name: self.raw[name] + suffix})


class CLITests(unittest.TestCase):
    def test_failed_final_maximum_request_returns_failure(self):
        class StopFailure(FakeIDRAC):
            def set_floor(self, value):
                if self.writes:
                    self.writes.append(value)
                    raise fan.ControlError('synthetic stop failure')
                return super().set_floor(value)
        with tempfile.TemporaryDirectory() as tmp:
            c = dataclasses.replace(config(), lock_file=Path(tmp) / 'lock', status_file=Path(tmp) / 'status.json')
            idrac = StopFailure()
            with patch('nvidia_idrac_fan.load_config', return_value=c), \
                 patch('nvidia_idrac_fan.IDRAC', return_value=idrac), \
                 patch('nvidia_idrac_fan.Controller.read_gpus', return_value=[gpu(), gpu('GPU-synthetic-b')]), \
                 patch('nvidia_idrac_fan.signal.signal'), patch('nvidia_idrac_fan.notify'), \
                 patch.dict('os.environ', {}, clear=True), contextlib.redirect_stdout(io.StringIO()):
                code = fan.main(['--config', '/synthetic/config.json', '--apply', '--once'])
            status = json.loads(c.status_file.read_text())
            self.assertEqual(code, 1)
            self.assertTrue(status['fault'])
            self.assertFalse(status['control_verified'])
            self.assertFalse(status['controller_running'])

    def test_successful_graceful_stop_leaves_verified_maximum(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = dataclasses.replace(config(), lock_file=Path(tmp) / 'lock', status_file=Path(tmp) / 'status.json')
            idrac = FakeIDRAC()
            with patch('nvidia_idrac_fan.load_config', return_value=c), \
                 patch('nvidia_idrac_fan.IDRAC', return_value=idrac), \
                 patch('nvidia_idrac_fan.Controller.read_gpus', return_value=[gpu(), gpu('GPU-synthetic-b')]), \
                 patch('nvidia_idrac_fan.signal.signal'), patch('nvidia_idrac_fan.notify'), \
                 patch.dict('os.environ', {}, clear=True), contextlib.redirect_stdout(io.StringIO()):
                code = fan.main(['--config', '/synthetic/config.json', '--apply', '--once'])
            status = json.loads(c.status_file.read_text())
            self.assertEqual(code, 0)
            self.assertEqual(status['verified_floor_pct'], 100)
            self.assertFalse(status['controller_running'])


if __name__ == '__main__':
    unittest.main()
