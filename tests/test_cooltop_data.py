"""Behavioral tests for a read-only dashboard: time, freshness, safe input, history."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cooltop_data import Collector, History, normalize, read_json

BOOT = '11111111-1111-1111-1111-111111111111'


def fixture():
    return {
        'utc': '2026-01-01T00:00:00+00:00', 'monotonic': 100, 'boot_id': BOOT,
        'requested_floor_pct': 55, 'reason': 'idle-temperature-control', 'control_path': 'usb',
        'controller_running': True, 'control_verified': True, 'control_degraded': False, 'fault_latched': False,
        'gpus': [{'gpu': 0, 'core_c': 40, 'hbm_c': 41, 'power_w': 39, 'util_pct': 0}],
        'cooling_diagnostics': {
            'configured_cutoffs_c': {'core': 80, 'hbm': 85},
            'setting_proof': {'fresh': True, 'age_seconds': 20, 'max_age_seconds': 90, 'verified': True},
            'physical_diagnostics': {
                'fresh': True, 'age_seconds': 20, 'max_age_seconds': 90,
                'fan_pwm_verified': True, 'fan_rpm_verified': None,
                'fans': [{'fan_id': 1, 'rpm': 10440, 'pwm_pct': 55, 'health': 'Ok'}],
                'temperatures': [{'kind': 'inlet', 'index': None, 'temperature_c': 24, 'available': True}],
            },
            'gpu_diagnostics': [{'gpu': 0, 'age_seconds': 0.2, 'fresh': True}],
        },
    }


def guard_fixture():
    return {'boot_id': BOOT, 'monotonic': 100.8, 'healthy': True,
            'gpus': [{'gpu': 0, 'core_c': 43, 'hbm_c': 44}]}


def snapshot(raw=None, guard=None, mono=101):
    return normalize(raw if raw is not None else fixture(), guard if guard is not None else guard_fixture(),
                     boot=BOOT, mono=mono, wall=1767225601)


class FreshnessTests(unittest.TestCase):
    def test_guard_temperatures_are_independent_of_slow_bmc_cycle(self):
        guard = guard_fixture(); guard['monotonic'] = 119.5
        result = snapshot(guard=guard, mono=120)
        self.assertTrue(result['guard']['fresh'])
        self.assertTrue(result['gpus'][0]['core_fresh'])
        self.assertFalse(result['gpus'][0]['stats_fresh'])
        self.assertEqual(result['gpus'][0]['hbm_c'], 44)
        self.assertEqual(result['gpus'][0]['core_source'], 'guard')

    def test_embedded_chassis_and_setting_age_advance_on_redraw(self):
        result = snapshot(mono=175)
        self.assertEqual(result['cooling']['physical_age_seconds'], 95)
        self.assertFalse(result['cooling']['physical_fresh'])
        self.assertIsNone(result['cooling']['fan_pwm_verified'])
        self.assertFalse(result['fans'][0]['fresh'])
        self.assertIsNone(result['cooling']['control_verified'])

    def test_guard_previous_boot_cannot_replace_valid_current_samples(self):
        guard = guard_fixture(); guard['boot_id'] = 'old'
        result = snapshot(guard=guard)
        self.assertEqual(result['guard']['state'], 'previous-boot')
        self.assertEqual(result['gpus'][0]['core_c'], 40)
        self.assertEqual(result['gpus'][0]['core_source'], 'watchdog')

    def test_old_boot_status_never_claims_current_fan_feedback(self):
        raw = fixture(); raw['boot_id'] = 'old'
        result = snapshot(raw=raw)
        self.assertEqual(result['status']['state'], 'previous-boot')
        self.assertFalse(result['fans'][0]['fresh'])
        self.assertFalse(result['cooling']['setting_fresh'])
        self.assertTrue(result['gpus'][0]['core_fresh'])

    def test_future_timestamp_rejected(self):
        result = snapshot(mono=99)
        self.assertEqual(result['status']['state'], 'future')
        self.assertFalse(result['fans'][0]['fresh'])
        self.assertFalse(result['gpus'][0]['core_fresh'])

    def test_stale_guard_falls_back_to_fresh_watchdog(self):
        guard = guard_fixture(); guard['monotonic'] = 20
        result = snapshot(guard=guard)
        self.assertEqual(result['gpus'][0]['hbm_c'], 41)
        self.assertTrue(result['gpus'][0]['core_fresh'])

    def test_unknown_and_bad_numbers_are_not_zero_measurements(self):
        raw = fixture(); raw['gpus'][0].update(core_c=True, hbm_c=float('nan'), power_w=float('inf'), util_pct=101)
        guard = guard_fixture(); guard['gpus'] = []
        gpu = snapshot(raw=raw, guard=guard)['gpus'][0]
        self.assertIsNone(gpu['core_c']); self.assertIsNone(gpu['hbm_c'])
        self.assertIsNone(gpu['power_w']); self.assertIsNone(gpu['util_pct'])
        self.assertEqual(snapshot()['gpus'][0]['util_pct'], 0)

    def test_sensor_unavailable_is_not_fresh(self):
        raw = fixture(); raw['cooling_diagnostics']['physical_diagnostics']['temperatures'][0]['available'] = False
        self.assertFalse(snapshot(raw=raw)['temperatures'][0]['fresh'])

    def test_setting_false_not_overridden_by_controller_boolean(self):
        raw = fixture(); raw['cooling_diagnostics']['setting_proof']['verified'] = False
        self.assertFalse(snapshot(raw=raw)['cooling']['control_verified'])

    def test_unknown_schema_is_invalid(self):
        self.assertEqual(snapshot(raw={'schema': 1})['status']['state'], 'invalid')


class SafeInputTests(unittest.TestCase):
    def test_missing_malformed_duplicate_and_oversize_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'status'
            self.assertEqual(read_json(p), (None, 'missing'))
            for text in ('{', '[]', '{"x":1,"x":2}', ' ' * (1024 * 1024 + 1)):
                p.write_text(text)
                self.assertEqual(read_json(p), (None, 'invalid'))
            p.write_text('{"x":2}')
            self.assertEqual(read_json(p), ({'x': 2}, None))

    def test_fifo_symlink_and_directory_are_never_read_as_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            os.mkfifo(tmp / 'fifo')
            self.assertEqual(read_json(tmp / 'fifo'), (None, 'invalid'))
            (tmp / 'real').write_text('{}')
            (tmp / 'link').symlink_to(tmp / 'real')
            self.assertEqual(read_json(tmp / 'link'), (None, 'invalid'))
            self.assertEqual(read_json(tmp), (None, 'invalid'))

    def test_terminal_control_characters_are_removed_from_strings(self):
        raw = fixture(); raw['reason'] = '\x1b[31mBAD\n\x07'
        self.assertNotIn('\x1b', snapshot(raw=raw)['cooling']['reason'])
        self.assertNotIn('\n', snapshot(raw=raw)['cooling']['reason'])


class HistoryTests(unittest.TestCase):
    def test_repeated_cached_samples_do_not_invent_history(self):
        history = History()
        history.add('fan', 90, 55, 'same', 100)
        history.add('fan', 90, 55, 'same', 101)
        history.add('fan', 91, 55, 'same', 102)
        self.assertEqual(len(history.values['fan']), 1)
        self.assertEqual(sum(v is not None for v in history.bins('fan', 110, 60, 60)), 1)

    def test_out_of_order_future_and_expired_samples_ignored(self):
        history = History(seconds=100)
        history.add('x', 90, 1, 1, 100)
        for observed, token in ((89, 2), (101, 3), (-1, 4)):
            history.add('x', observed, 9, token, 100)
        self.assertEqual(list(history.values['x']), [(90, 1)])
        self.assertEqual(history.bins('x', 200, 100, 10), [None] * 10)

    def test_bounded_samples_and_missing_bins(self):
        history = History(seconds=1000, max_samples=10)
        for i in range(30):
            history.add('x', i, i, i, i)
        self.assertEqual(len(history.values['x']), 10)
        self.assertIsNone(history.bins('x', 100, 100, 10)[-1])
        self.assertEqual(history.bins('absent', 100, 100, 4), [None] * 4)


    def test_step_hold_uses_original_remaining_ttl_not_ingestion_time(self):
        history = History()
        history.add('fan', 80, 55, 'sample', 100, ttl=30)
        history.add('fan', 80, 55, 'sample', 105, ttl=30)
        self.assertEqual(len(history.values['fan']), 1)
        bins = history.bins('fan', 120, 40, 40)
        self.assertEqual(bins[:30], [55] * 30)
        self.assertEqual(bins[30:], [None] * 10)
        history.add('fan', 80, 55, 'sample', 120, ttl=30)
        self.assertEqual(history.bins('fan', 140, 20, 20), [None] * 20)

    def test_error_and_cached_recovery_leave_gap_and_original_expiry(self):
        history = History()
        history.add('fan', 80, 55, 'sample', 90, ttl=30)
        history.invalidate('fan', 100)
        history.add('fan', 80, 55, 'sample', 105, ttl=30)
        bins = history.bins('fan', 120, 40, 40)
        self.assertEqual(bins[:20], [55] * 20)
        self.assertEqual(bins[20:25], [None] * 5)
        self.assertEqual(bins[25:30], [55] * 5)
        self.assertEqual(bins[30:], [None] * 10)
        self.assertEqual(len(history.values['fan']), 1)

    def test_new_observation_after_error_may_resume_at_measurement_time(self):
        history = History()
        history.add('fan', 80, 55, 'old', 90, ttl=30)
        history.invalidate('fan', 100)
        history.add('fan', 104, 60, 'new', 105, ttl=10)
        bins = history.bins('fan', 120, 40, 40)
        self.assertEqual(bins[20:24], [None] * 4)
        self.assertEqual(bins[24:34], [60] * 10)
        self.assertEqual(bins[34:], [None] * 6)

    def test_peak_aggregation_preserves_brief_temperature_spike(self):
        history = History()
        for observed, value in ((80, 40), (85, 80), (86, 45)):
            history.add('gpu', observed, value, observed, observed, ttl=20)
        self.assertEqual(history.bins('gpu', 90, 20, 1), [80])
        self.assertEqual(history.bins('gpu', 90, 20, 1, 'last'), [45])
        self.assertEqual(history.bins('gpu', 90, 20, 1, 'min'), [40])

    def test_older_alternate_sensor_starts_at_switch_without_backfill(self):
        history = History()
        history.add('core', 100, 50, ('guard', 100), 100, ttl=10, source='guard')
        history.add('core', 98, 45, ('nvidia', 98), 102, ttl=15, source='nvidia')
        bins = history.bins('core', 110, 10, 10)
        self.assertEqual(bins[:2], [50, 50])
        self.assertEqual(bins[2:], [45] * 8)
        self.assertEqual(list(history.values['core']), [(100, 50), (98, 45)])


class CollectorTests(unittest.TestCase):
    def test_cached_default_starts_no_external_process_and_dedupes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'sys/kernel/random').mkdir(parents=True)
            (root / 'sys/kernel/random/boot_id').write_text(BOOT)
            (root / 'status').write_text(json.dumps(fixture()))
            (root / 'guard').write_text(json.dumps(guard_fixture()))
            (root / 'stat').write_text('cpu  100 0 100 700 100 0 0 0\n')
            (root / 'meminfo').write_text('MemTotal: 102400 kB\nMemAvailable: 25600 kB\nSwapTotal: 1024 kB\nSwapFree: 512 kB\n')
            (root / 'loadavg').write_text('1.1 2.2 3.3 1/20 123')
            (root / 'uptime').write_text('1000.5 10')
            with mock.patch('cooltop_data.subprocess.run') as run:
                c = Collector(root / 'status', root / 'guard', proc_root=root)
                with mock.patch('cooltop_data.time.monotonic', return_value=101):
                    first = c.sample()
                with mock.patch('cooltop_data.time.monotonic', return_value=102):
                    second = c.sample()
                c.close()
                run.assert_not_called()
            self.assertEqual(first['system']['mem_pct'], 75)
            self.assertEqual(first['system']['load5'], 2.2)
            self.assertEqual(first['system']['uptime_s'], 1000.5)
            self.assertEqual(len(c._history.values['gpu.0.core_c']), 1)
            self.assertEqual(len(c._history.values['fan.1.rpm']), 1)
            self.assertTrue(second['fans'][0]['fresh'])

    def test_missing_physical_source_ends_hold_at_detection_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'sys/kernel/random').mkdir(parents=True)
            (root / 'sys/kernel/random/boot_id').write_text(BOOT)
            (root / 'status').write_text(json.dumps(fixture()))
            (root / 'guard').write_text(json.dumps(guard_fixture()))
            c = Collector(root / 'status', root / 'guard', proc_root=root)
            with mock.patch('cooltop_data.time.monotonic', return_value=101):
                c.sample()
            (root / 'status').unlink()
            with mock.patch('cooltop_data.time.monotonic', return_value=110):
                c.sample()
            bins = c._history.bins('fan.1.pwm_pct', 120, 40, 40)
            self.assertEqual(bins[:30], [55] * 30)
            self.assertEqual(bins[30:], [None] * 10)
            self.assertEqual(len(c._history.values['fan.1.pwm_pct']), 1)
            c.close()

    def test_physical_observation_identity_survives_publication_clock_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'sys/kernel/random').mkdir(parents=True)
            (root / 'sys/kernel/random/boot_id').write_text(BOOT)
            raw = fixture()
            physical = raw['cooling_diagnostics']['physical_diagnostics']
            physical['observed_time_unix'] = 1767225580
            (root / 'status').write_text(json.dumps(raw))
            (root / 'guard').write_text(json.dumps(guard_fixture()))
            c = Collector(root / 'status', root / 'guard', proc_root=root)
            with mock.patch('cooltop_data.time.monotonic', return_value=101):
                c.sample()
            raw['monotonic'] = 110
            physical['age_seconds'] = 30.003
            (root / 'status').write_text(json.dumps(raw))
            with mock.patch('cooltop_data.time.monotonic', return_value=111):
                c.sample()
            self.assertEqual(len(c._history.values['fan.1.pwm_pct']), 1)
            self.assertEqual(c._history._segments['fan.1.pwm_pct'][0][2], 170)
            c.close()

    def test_missing_files_are_reported_without_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = Collector(Path(tmp) / 'no-status', Path(tmp) / 'no-guard', proc_root=tmp)
            result = c.sample(); c.close()
            self.assertEqual(result['status']['state'], 'missing')
            self.assertEqual(result['guard']['state'], 'missing')
            self.assertIsNone(result['system']['mem_pct'])
            self.assertEqual(result['gpus'], [])

    def test_standalone_details_discover_gpu_without_controller_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c = Collector(root / 'missing', root / 'missing-guard', proc_root=root)
            c.details = mock.Mock()
            c.details.read.return_value = (100, {0: {'name': 'Tesla V100', 'memory_used_mb': 26000,
                'memory_total_mb': 32768, 'power_limit_w': 250, 'power_w': 40, 'util_pct': 0, 'core_c': 43}}, None)
            with mock.patch('cooltop_data.time.monotonic', return_value=101):
                result = c.sample()
            gpu = result['gpus'][0]
            self.assertEqual(result['status']['state'], 'missing')
            self.assertEqual(gpu['core_c'], 43)
            self.assertTrue(gpu['core_fresh'])
            self.assertFalse(gpu['hbm_fresh'])
            self.assertIsNone(gpu['hbm_c'])
            self.assertEqual(gpu['stats_source'], 'nvidia-smi')
            self.assertEqual(gpu['power_limit_w'], 250)
            c.close()

    def test_stale_details_do_not_discover_live_gpu_or_replace_fresh_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'status').write_text(json.dumps(fixture()))
            (root / 'guard').write_text(json.dumps(guard_fixture()))
            (root / 'sys/kernel/random').mkdir(parents=True)
            (root / 'sys/kernel/random/boot_id').write_text(BOOT)
            c = Collector(root / 'status', root / 'guard', proc_root=root)
            c.details = mock.Mock()
            c.details.read.return_value = (80, {0: {'core_c': 100, 'power_w': 250, 'util_pct': 100}, 1: {'name': 'stale GPU'}}, None)
            with mock.patch('cooltop_data.time.monotonic', return_value=101):
                result = c.sample()
            self.assertEqual(len(result['gpus']), 1)
            gpu = result['gpus'][0]
            self.assertEqual(gpu['core_c'], 43)
            self.assertEqual(gpu['power_w'], 39)
            self.assertFalse(gpu['details_fresh'])
            self.assertEqual(gpu['core_source'], 'guard')
            c.close()

    def test_details_for_absent_gpu_are_not_claimed_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'status').write_text(json.dumps(fixture())); (root / 'guard').write_text(json.dumps(guard_fixture()))
            c = Collector(root / 'status', root / 'guard', proc_root=root)
            c.details = mock.Mock()
            c.details.read.return_value = (100, {1: {'name': 'another GPU'}}, None)
            with mock.patch('cooltop_data.time.monotonic', return_value=101):
                result = c.sample()
            self.assertFalse(result['gpus'][0]['details_fresh'])
            c.close()


if __name__ == '__main__':
    unittest.main()
