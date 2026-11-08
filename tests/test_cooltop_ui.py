"""Read-only display regressions: resizing, stale evidence, and terminal safety."""
import unittest

from cooltop import Canvas, help_overlay, render_dashboard


def snapshot():
    return {
        'sampled_unix': 1800000000,
        'system': {'hostname': 'example-host', 'uptime_s': 7200, 'cpu_pct': 20,
                   'mem_pct': 50, 'mem_used_mb': 8192, 'mem_total_mb': 16384},
        'status': {'fresh': True, 'state': 'fresh', 'age_seconds': 1},
        'guard': {'fresh': True, 'age_seconds': 1},
        'cooling': {'controller_running': True, 'control_verified': True,
                    'setting_fresh': True, 'guard_healthy': True,
                    'requested_floor_pct': 55, 'idle_min_pct': 50, 'idle_max_pct': 80,
                    'cutoff_core_c': 80, 'cutoff_hbm_c': 85,
                    'physical_age_seconds': 3, 'reason': 'idle-temperature-control'},
        'gpus': [dict(index=i, name='Example GPU', core_c=40, hbm_c=43,
                      core_fresh=True, stats_fresh=True, details_fresh=True,
                      core_age_seconds=1, stats_age_seconds=1,
                      util_pct=0, power_w=39, power_limit_w=250,
                      memory_used_mb=8192, memory_total_mb=32768,
                      sm_clock_mhz=1200, memory_clock_mhz=877)
                 for i in range(2)],
        'fans': [dict(fan_id=i, rpm=10000, pwm_pct=55, fresh=True, healthy=True)
                 for i in range(1, 7)],
        'temperatures': [dict(kind='inlet', index=0, temperature_c=24, fresh=True)],
    }


class Histories:
    def history(self, key, seconds, width):
        return [None if i % 3 == 0 else i * 100 / max(1, width) for i in range(width)]


class CooltopDisplayTests(unittest.TestCase):
    def test_resize_and_help_clip_without_dropping_exit_instruction(self):
        for width, height in [(70, 23), (80, 24), (120, 36), (240, 100), (3, 1), (1, 1)]:
            with self.subTest(size=(width, height)):
                canvas = render_dashboard(snapshot(), Histories(), width=width, height=height)
                self.assertEqual(len(canvas.cells), height)
                self.assertTrue(all(len(row) == width for row in canvas.cells))
                self.assertTrue(all(len(line) <= width for line in canvas.text().splitlines()))
                if width >= 70:
                    self.assertIn('q quit', canvas.text())
                help_overlay(canvas)
                self.assertTrue(all(len(line) <= width for line in canvas.text().splitlines()))

    def test_stale_sources_remain_explicit_in_uncolored_snapshot(self):
        data = snapshot()
        data['status'].update(fresh=False, state='previous-boot', age_seconds=None)
        for gpu in data['gpus']:
            gpu.update(stats_fresh=False, details_fresh=False)
        for fan in data['fans']:
            fan['fresh'] = False
        data['temperatures'][0]['fresh'] = False
        text = render_dashboard(data, width=120, height=36).text()
        self.assertIn('STALE CONTROLLER STATUS', text)
        self.assertNotIn('DYNAMIC CONTROL', text)
        self.assertIn('LOAD 0%*', text)
        self.assertIn('39W*', text)
        self.assertIn('* VRAM', text)
        self.assertIn('55%*', text)
        self.assertIn('INLET 24C*', text)
        self.assertIn('Guard HEALTHY', text)  # Fresh guard is separate from old stats.

    def test_unconfirmed_controller_is_not_reported_as_dynamic(self):
        data = snapshot()
        data['cooling']['controller_running'] = False
        text = render_dashboard(data, width=120, height=36).text()
        self.assertIn('CONTROL UNCONFIRMED', text)
        self.assertNotIn('DYNAMIC CONTROL', text)

    def test_telemetry_cannot_inject_terminal_controls(self):
        data = snapshot()
        injected = 'text\x1b[2J\x00\r\n\t\x07'
        data['system']['hostname'] = injected
        data['cooling']['reason'] = injected
        data['gpus'][0]['name'] = injected
        for ascii_only in (False, True):
            text = render_dashboard(data, width=120, height=36, ascii_only=ascii_only).text()
            self.assertTrue(all(c == '\n' or c.isprintable() for c in text))
            self.assertNotIn('\x1b', text)
            if ascii_only:
                text.encode('ascii')

    def test_missing_telemetry_renders_an_explanation(self):
        text = render_dashboard({}, width=120, height=36).text()
        self.assertIn('No telemetry', text)
        self.assertIn('No chassis readings', text)
        self.assertIn('STALE CONTROLLER STATUS', text)


if __name__ == '__main__':
    unittest.main()
