"""Read-only display regressions: resizing, stale evidence, and terminal safety."""
import unittest

from cooltop import BLUE, CYAN, Canvas, chart, help_overlay, render_dashboard


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
        for width, height in [(70, 23), (80, 24), (120, 36), (120, 40), (120, 48), (80, 60), (160, 60), (240, 100), (3, 1), (1, 1)]:
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
        for height in (36, 48):
            with self.subTest(height=height):
                text = render_dashboard(data, width=120, height=height).text()
                self.assertIn('STALE CONTROLLER STATUS', text)
                self.assertNotIn('DYNAMIC CONTROL', text)
                self.assertIn('LOAD 0%*', text)
                self.assertIn('39W*', text)
                self.assertRegex(text, r'(?:\*\s+VRAM[^\n]*GiB|VRAM[^\n]*GiB\*)')
                self.assertIn('55%*', text)
                self.assertRegex(text, r'IN(?:LET)? 24C\*')
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
        self.assertRegex(text, r'(No chassis readings|Waiting for cached iDRAC fan readings)')
        self.assertIn('STALE CONTROLLER STATUS', text)

    def test_compact_layout_keeps_both_gpus_and_all_six_fan_values(self):
        text = render_dashboard(snapshot(), Histories(), width=80, height=24).text()
        self.assertIn('GPU 0', text)
        self.assertIn('GPU 1', text)
        for fan_id in range(1, 7):
            self.assertRegex(text, rf'(?m)^\s*[│|]\s+{fan_id}\s+10000\s+55%')
        self.assertIn('Guard HEALTHY', text)

    def test_larger_windows_allocate_extra_height_to_charts(self):
        chart_rows = []
        fan_panel_rows = []
        for width, height in [(120, 48), (160, 60), (240, 100)]:
            canvas = render_dashboard(snapshot(), Histories(), width=width, height=height)
            lines = canvas.text().splitlines()
            self.assertEqual(sum('TEMPERATURE' in line and 'CHASSIS' not in line for line in lines), 1)
            self.assertEqual(sum('GPU UTILIZATION' in line for line in lines), 1)
            self.assertEqual(sum('BOARD POWER' in line for line in lines), 1)
            self.assertIn('-5m', canvas.text())
            self.assertIn('now', canvas.text())
            first_fan_row = next(i for i, line in enumerate(lines) if 'CHASSIS FANS' in line)
            fan_panel_rows.append(first_fan_row)
            data_rows = sum(any(char in ('●', '▄', '◆') or 0x2800 <= ord(char) <= 0x28ff
                                for char, style in row)
                            for row in canvas.cells[4:first_fan_row])
            chart_rows.append(data_rows)
            self.assertGreater(first_fan_row, height // 2)
            # Large unused blocks would recreate the original fixed-height view.
            self.assertGreater(data_rows, (first_fan_row - 4) // 3)
        self.assertTrue(all(a < b for a, b in zip(chart_rows, chart_rows[1:])))
        self.assertTrue(all(a < b for a, b in zip(fan_panel_rows, fan_panel_rows[1:])))

    def test_chart_missing_values_are_not_drawn_as_zero(self):
        missing, zero = Canvas(50, 14), Canvas(50, 14)
        chart(missing, 0, 0, 50, 14, 'LOAD', [('GPU 0', [None] * 44, CYAN)], 100, 300, unit='%', area=True)
        chart(zero, 0, 0, 50, 14, 'LOAD', [('GPU 0', [0] * 44, CYAN)], 100, 300, unit='%', area=True)
        self.assertIn('Awaiting fresh samples', missing.text())
        self.assertNotIn('Awaiting fresh samples', zero.text())
        self.assertFalse(any(char == '▄' for row in missing.cells for char, _ in row))
        self.assertTrue(any(char == '▄' for row in zero.cells for char, _ in row))

    def test_chart_has_numeric_time_axes_and_distinct_series_colors(self):
        canvas = Canvas(60, 16)
        chart(canvas, 0, 0, 60, 16, 'TEMPERATURE', [
            ('GPU 0', [20] * 54, CYAN), ('GPU 1', [80] * 54, BLUE),
        ], 100, 300, unit='C')
        text = canvas.text()
        self.assertIn('100', text)
        self.assertRegex(text, r'(?m)^\s*0\s*[┤|]')
        for label in ('-5m', '-2m30s', 'now'):
            self.assertIn(label, text)
        colored_points = {style for row in canvas.cells for char, style in row if char != ' ' and style in (CYAN, BLUE)}
        self.assertTrue({CYAN, BLUE}.issubset(colored_points))

    def test_smallest_chart_keeps_true_minimum_and_maximum_ticks(self):
        canvas = Canvas(50, 4)
        chart(canvas, 0, 0, 50, 4, 'LOAD', [('GPU 0', [0] * 44, CYAN)], 100, 300, unit='%', area=True)
        lines = canvas.text().splitlines()
        self.assertRegex(lines[1], r'^\s*100\s*[┤|]')
        self.assertRegex(lines[2], r'^\s*0\s*[┤|]')
        self.assertNotRegex(lines[2], r'^\s*50\s*[┤|]')

    def test_chart_does_not_draw_a_line_across_missing_columns(self):
        canvas = Canvas(50, 14)
        values = [20] * 10 + [None] * 24 + [80] * 10
        chart(canvas, 0, 0, 50, 14, 'LOAD', [('GPU 0', values, CYAN)], 100, 300)
        # Six columns form the y-axis; the middle missing interval must have
        # no data-colored pixels, even though grid and time labels remain.
        for row in canvas.cells[1:-1]:
            self.assertFalse(any(style == CYAN and char != ' ' for char, style in row[16:40]))

    def test_braille_preserves_small_core_hbm_temperature_difference(self):
        masks = []
        for values in ([40], [43], [40, 43]):
            canvas = Canvas(50, 8)
            series = [(str(value), [value] * 44, CYAN if i == 0 else BLUE)
                      for i, value in enumerate(values)]
            chart(canvas, 0, 0, 50, 8, 'TEMPERATURE', series, 100, 300, unit='C')
            masks.append({i: ord(row[20][0]) - 0x2800 for i, row in enumerate(canvas.cells)
                          if 0x2800 <= ord(row[20][0]) <= 0x28ff})
        core, hbm, combined = masks
        self.assertTrue(core)
        self.assertTrue(hbm)
        self.assertNotEqual(core, hbm)  # 40 C and 43 C must not collapse to one dot.
        for row in core.keys() | hbm.keys():
            self.assertEqual(combined[row], core.get(row, 0) | hbm.get(row, 0))

    def test_missing_hbm_is_not_reported_as_core_temperature_or_zero(self):
        data = snapshot()
        data['gpus'][0].update(hbm_c=None, hbm_fresh=False, core_fresh=True)
        for height in (36, 48):
            text = render_dashboard(data, width=120, height=height).text()
            self.assertIn('CORE 40°C', text)
            self.assertIn('HBM --*', text)


if __name__ == '__main__':
    unittest.main()
