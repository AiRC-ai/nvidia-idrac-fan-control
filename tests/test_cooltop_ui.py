"""Read-only display regressions: resizing, stale evidence, and terminal safety."""
import types
import unittest
from unittest.mock import patch

import cooltop
from cooltop import BAD, BLUE, CYAN, GOOD, PURPLE, WARN, Canvas, chart, gpu_summary, help_overlay, paired_history, parse_args, render_dashboard, window_range, workload_overview


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
    def history(self, key, seconds, width, aggregation="max"):
        return [None if i % 3 == 0 else i * 100 / max(1, width) for i in range(width)]


class CooltopDisplayTests(unittest.TestCase):
    def test_resize_and_help_clip_without_dropping_exit_instruction(self):
        for width, height in [(70, 23), (80, 24), (120, 36), (120, 40), (120, 48), (80, 60), (160, 60), (240, 100), (3, 1), (1, 1)]:
            for view in ("overview", "detail"):
                with self.subTest(size=(width, height), view=view):
                    canvas = render_dashboard(snapshot(), Histories(), width=width, height=height, view=view)
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
        for view in ("overview", "detail"):
            for height in (36, 60):
                with self.subTest(height=height, view=view):
                    canvas = render_dashboard(data, width=160, height=height, view=view)
                    text = canvas.text()
                    self.assertIn('STALE CONTROLLER STATUS', text)
                    self.assertNotIn('DYNAMIC CONTROL', text)
                    self.assertIn('LOAD 0%*', text)
                    self.assertIn('39W*', text)
                    self.assertRegex(text, r'(?:\*\s+VRAM[^\n]*G|VRAM[^\n]*G(?:iB)?\*)')
                    self.assertIn('55%*', text)
                    self.assertRegex(text, r'IN(?:LET)? 24C\*')
                    self.assertIn('Guard HEALTHY', text)  # Fresh guard is separate from old stats.
                    for row in canvas.cells:
                        line = ''.join(char for char, _ in row)
                        if 'STALE CONTROLLER STATUS' in line:
                            start = line.index('STALE CONTROLLER STATUS')
                            self.assertNotIn(GOOD, [style for _, style in row[start:start + 23]])

    def test_unconfirmed_controller_is_not_reported_as_dynamic(self):
        data = snapshot()
        data['cooling']['controller_running'] = False
        text = render_dashboard(data, width=120, height=36).text()
        self.assertIn('CONTROL UNCONFIRMED', text)
        self.assertNotIn('DYNAMIC CONTROL', text)

    def test_missing_or_stale_guard_does_not_look_healthy(self):
        for guard in ({}, {'fresh': False, 'age_seconds': 300}):
            for view in ('overview', 'detail'):
                with self.subTest(guard=guard, view=view):
                    data = snapshot()
                    data['guard'] = guard
                    text = render_dashboard(data, width=160, height=60, view=view).text()
                    self.assertIn('Guard UNCONFIRMED', text)
                    self.assertNotIn('Guard HEALTHY', text)

    def test_degraded_or_faulted_control_cannot_render_healthy_green(self):
        for field, label in (('control_degraded', 'DEGRADED'), ('fault_latched', 'FAULT LATCHED')):
            for view in ('overview', 'detail'):
                with self.subTest(field=field, view=view):
                    data = snapshot()
                    data['cooling'][field] = True
                    canvas = render_dashboard(data, width=160, height=60, view=view)
                    self.assertIn(label, canvas.text())
                    for row in canvas.cells:
                        line = ''.join(char for char, _ in row)
                        if label in line:
                            start = line.index(label)
                            styles = [style for _, style in row[start:start + len(label)]]
                            self.assertNotIn(GOOD, styles)
                            if field == 'fault_latched':
                                self.assertEqual(set(styles), {BAD})

    def test_known_bad_fan_cannot_make_aggregate_feedback_green(self):
        data = snapshot()
        data['fans'][0]['healthy'] = False
        for view in ('overview', 'detail'):
            canvas = render_dashboard(data, width=160, height=60, view=view)
            for row in canvas.cells:
                line = ''.join(char for char, _ in row)
                if 'Actual PWM' in line:
                    start = line.index('Actual PWM')
                    self.assertNotIn(GOOD, [style for _, style in row[start:start + 20]])

    def test_current_temperature_summary_warns_at_real_guard_thresholds(self):
        for core, hbm, expected in ((75, 80, WARN), (85, 90, BAD)):
            with self.subTest(core=core, hbm=hbm):
                data = snapshot()
                for gpu in data['gpus']:
                    gpu.update(core_c=core, hbm_c=hbm)
                canvas = render_dashboard(data, width=160, height=60, view='overview')
                for label, value in (('CORE', core), ('HBM', hbm)):
                    marker = f'{label} {value}'
                    # The first current reading is in the summary; histories
                    # may have their own semantic trace colors and old peaks.
                    row = next(row for row in canvas.cells if marker in ''.join(char for char, _ in row))
                    line = ''.join(char for char, _ in row)
                    start = line.index(marker)
                    self.assertEqual({style for _, style in row[start:start + len(marker)]}, {expected})

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

    def test_overview_prioritizes_both_gpus_and_all_six_measured_fans(self):
        for width, height in ((80, 24), (120, 40), (160, 60), (240, 100)):
            with self.subTest(size=(width, height)):
                text = render_dashboard(snapshot(), Histories(), width=width, height=height).text()
                self.assertIn('GPU 0', text)
                self.assertIn('GPU 1', text)
                self.assertRegex(text, r'CORE 40°?C')
                self.assertRegex(text, r'HBM\s+43°?C')
                for fan_id in range(1, 7):
                    self.assertRegex(text, rf'[│|]\s+{fan_id}\s+10000\s+55%')
                if width >= 150:
                    # The overview must not push the feedback table below a
                    # screenful merely because the terminal has spare height.
                    early = '\n'.join(text.splitlines()[:36])
                    self.assertRegex(early, r'(?m)[│|]\s+6\s+10000\s+55%')

    def test_views_are_selectable_without_changing_snapshot_data(self):
        self.assertEqual(parse_args([]).view, 'overview')
        self.assertEqual(parse_args(['--view', 'detail']).view, 'detail')
        data = snapshot()
        overview = render_dashboard(data, Histories(), width=160, height=60, view='overview').text()
        detail = render_dashboard(data, Histories(), width=160, height=60, view='detail').text()
        self.assertNotEqual(overview, detail)
        for text in (overview, detail):
            for label in ('GPU 0', 'GPU 1', '55%', 'Guard HEALTHY'):
                self.assertIn(label, text)
            self.assertRegex(text, r'CORE 40°?C')
            self.assertRegex(text, r'HBM\s+43°?C')

    def test_view_and_history_changes_survive_pause_and_help(self):
        class Screen:
            def __init__(self):
                self.keys = iter(map(ord, ('v', 'p', '+', 'h', 'h', '\t', 'p', 'q')))

            def keypad(self, enabled):
                pass

            def timeout(self, interval):
                pass

            def getmaxyx(self):
                return 60, 160

            def getch(self):
                return next(self.keys)

        class Collector(Histories):
            def __init__(self):
                self.samples = 0

            def sample(self):
                self.samples += 1
                return snapshot()

        collector = Collector()
        seen = []

        def render(data, source, **options):
            seen.append((options['view'], options['paused'], options['span']))
            return Canvas(options['width'], options['height'])

        fake_curses = types.SimpleNamespace(curs_set=lambda _: None, has_colors=lambda: False, error=RuntimeError)
        with patch.dict('sys.modules', {'curses': fake_curses}), \
                patch.object(cooltop, 'render_dashboard', side_effect=render), \
                patch.object(cooltop, 'draw_curses'), \
                patch.object(cooltop.time, 'monotonic', return_value=100):
            cooltop.run_curses(Screen(), collector, parse_args(['--interval', '60']))
        self.assertEqual(seen, [
            ('overview', False, 300), ('detail', False, 300),
            ('detail', True, 300), ('detail', True, 900),
            ('detail', True, 900), ('detail', True, 900),
            ('overview', True, 900), ('overview', False, 900),
        ])
        self.assertEqual(collector.samples, 2)  # Initial read and resume; none while paused.

    def test_low_positive_load_is_not_labeled_zero_or_missing(self):
        for view in ('overview', 'detail'):
            for value in (0, 0.4, None):
                data = snapshot()
                for gpu in data['gpus']:
                    gpu['util_pct'] = value
                text = render_dashboard(data, width=160, height=60, view=view).text()
                label = 'LOAD --' if value is None else 'LOAD 0.4%' if value else 'LOAD 0%'
                self.assertIn(label, text)
                if value:
                    self.assertNotIn('LOAD 0%', text)
        # A visible positive segment is useful even below one cell's nominal
        # scale; missing and genuine zero must leave the current bar empty.
        for ascii_only in (False, True):
            bars = []
            for value in (0, 0.4, None):
                canvas = Canvas(20, 1, ascii_only)
                canvas.bar(0, 0, 20, value, 100, CYAN)
                bars.append(canvas.cells[0])
            self.assertNotEqual(bars[0], bars[1])
            self.assertEqual(bars[0], bars[2])

    def test_larger_windows_allocate_extra_height_to_charts(self):
        chart_rows = []
        fan_panel_rows = []
        for width, height in [(120, 48), (160, 60), (240, 100)]:
            canvas = render_dashboard(snapshot(), Histories(), width=width, height=height, view='detail')
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

    def test_window_range_uses_actual_minima_instead_of_minimum_peak(self):
        class Source:
            def history(self, key, seconds, width, aggregation='max'):
                return [None, 20, 40] if aggregation == 'min' else [None, 60, 80]

        class LegacyPeakOnly:
            def history(self, key, seconds, width):
                return [60, 80]

        self.assertEqual(window_range(Source(), 'gpu.0.core_c', 300), (20, 80))
        # A source exposing only peak buckets cannot establish a real minimum.
        self.assertEqual(window_range(LegacyPeakOnly(), 'gpu.0.core_c', 300), (None, None))

    def test_equal_paired_histories_preserve_both_colors_and_missing_data(self):
        class Source:
            def history(self, key, seconds, width, aggregation='max'):
                return [None] * (width - 2) + [None if key.endswith('hbm_c') else 50, 50]

        a, b = paired_history(Source(), 'gpu.0.core_c', 'gpu.0.hbm_c', 300, 44)
        canvas = Canvas(50, 10)
        chart(canvas, 0, 0, 50, 10, 'TEMPERATURE', [('CORE', a, CYAN), ('HBM', b, PURPLE)], 100, 300)
        colors = {style for row in canvas.cells[1:-1] for char, style in row
                  if 0x2800 <= ord(char) <= 0x28ff}
        self.assertTrue({CYAN, PURPLE}.issubset(colors))
        self.assertEqual(sum(v is not None for v in a), 2)
        self.assertEqual(sum(v is not None for v in b), 1)

    def test_power_percentage_trace_requires_a_fresh_known_cap(self):
        class Source:
            def history(self, key, seconds, width, aggregation='max'):
                return [100 if key.endswith('power_w') else 20] * width

        for cap, fresh in ((250, True), (250, False), (None, True), (0, True)):
            with self.subTest(cap=cap, details_fresh=fresh):
                gpu = snapshot()['gpus'][0]
                gpu.update(power_limit_w=cap, details_fresh=fresh)
                canvas = Canvas(80, 14)
                workload_overview(canvas, Source(), gpu, 0, 0, 80, 14, 300)
                power_points = [char for row in canvas.cells for char, style in row
                                if style == BLUE and 0x2800 <= ord(char) <= 0x28ff]
                self.assertEqual(bool(power_points), fresh and cap == 250)
                if cap == 250 and not fresh:
                    self.assertIn('250W*', canvas.text())

    def test_current_power_bar_also_requires_a_fresh_known_cap(self):
        for cap, fresh in ((250, True), (250, False), (None, True), (0, True)):
            with self.subTest(cap=cap, details_fresh=fresh):
                data = snapshot()
                gpu = data['gpus'][0]
                gpu.update(power_limit_w=cap, details_fresh=fresh)
                canvas = Canvas(80, 13)
                gpu_summary(canvas, data, None, gpu, 0, 0, 80, 13, 300)
                power_row = next(row for row in canvas.cells if 'POWER 39W' in ''.join(char for char, _ in row))
                filled = any(char in '▰▏▎▍▌▋▊▉+#' and style == BLUE for char, style in power_row)
                self.assertEqual(filled, fresh and cap == 250)
                # Absolute watts remain useful even when a current percent of
                # cap cannot be established from the older cached details.
                self.assertIn('POWER 39W', canvas.text())
                if cap == 250 and not fresh:
                    self.assertIn('CAP 250W*', canvas.text())

    def test_both_overview_floor_requests_are_marked_when_status_is_stale(self):
        data = snapshot()
        data['status'].update(fresh=False, state='stale', age_seconds=300)
        # Deliberately leave the old cached setting proof True. It must not
        # override the parent status source's expiry.
        text = render_dashboard(data, width=160, height=60, view='overview').text()
        floors = [line[line.index('FLOOR REQ'):] for line in text.splitlines() if 'FLOOR REQ' in line]
        self.assertGreaterEqual(len(floors), 2)
        for line in floors:
            self.assertIn('FLOOR REQ 55%*', line)

    def test_cached_cutoff_policy_is_not_presented_as_current(self):
        data = snapshot()
        data['status'].update(fresh=False, state='stale', age_seconds=300)
        text = render_dashboard(data, width=160, height=60, view='overview').text()
        # Independent fresh GPU/guard readings do not renew policy values
        # obtained from the expired watchdog status.
        for label in ('Guard cutoff', 'To guard cutoff', 'Idle band'):
            panels = [panel for line in text.splitlines() for panel in line.split('│') if label in panel]
            self.assertTrue(panels, label)
            for panel in panels:
                self.assertTrue('*' in panel or 'cached' in panel.lower(), panel)

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
