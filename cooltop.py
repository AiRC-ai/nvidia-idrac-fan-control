#!/usr/bin/env python3
"""cooltop: a read-only terminal dashboard for NVIDIA/iDRAC cooling telemetry.

No control commands, service changes, or BMC requests originate here. The
collector reads existing telemetry; optional GPU details use a bounded query.
"""
from __future__ import annotations

import argparse
import datetime as dt
import locale
import math
from pathlib import Path
import sys
import time

from cooltop_data import Collector

VERSION = "0.1.0"
DEFAULT_STATUS = "/var/lib/airc-gpu-fan-watchdog/status.json"
DEFAULT_GUARD = "/run/airc-gpu-thermal-guard/guard.json"
SPANS = (60, 300, 900)
NORMAL, DIM, TITLE, GOOD, WARN, BAD, CYAN, BLUE = range(8)


def clean(value):
    """Never send control sequences from telemetry into a terminal."""
    return "".join(c for c in str(value) if c.isprintable() and c not in "\u2028\u2029")


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def fmt(value, unit="", digits=0):
    return f"{value:.{digits}f}{unit}" if numeric(value) else "--"


def duration(value):
    if not numeric(value):
        return "--"
    value = max(0, int(value))
    if value >= 86400:
        return f"{value // 86400}d {value % 86400 // 3600}h"
    return f"{value // 3600}:{value % 3600 // 60:02d}:{value % 60:02d}"


def age(value):
    if not numeric(value):
        return "unknown age"
    return f"{value:.1f}s old" if value < 60 else f"{value / 60:.1f}m old"


def temperature_style(value, cutoff):
    if not numeric(value):
        return DIM
    return BAD if value >= cutoff else WARN if value >= cutoff - 10 else GOOD


class Canvas:
    """Small clipping canvas shared by curses and reproducible plain snapshots."""
    def __init__(self, width, height, ascii_only=False):
        self.width, self.height = max(1, width), max(1, height)
        self.ascii = ascii_only
        self.cells = [[(" ", NORMAL) for _ in range(self.width)] for _ in range(self.height)]

    def put(self, y, x, text, style=NORMAL):
        if not 0 <= y < self.height:
            return
        text = clean(text)
        if self.ascii:
            text = text.translate(str.maketrans({"°": " ", "·": ".", "—": "-", "–": "-", "…": "~", "●": "*", "│": "|", "─": "-", "┌": "+", "┐": "+", "└": "+", "┘": "+"}))
            text = text.encode("ascii", "replace").decode("ascii")
        for i, character in enumerate(text):
            if 0 <= x + i < self.width:
                self.cells[y][x + i] = (character, style)

    def rule(self, y, x, width, style=DIM):
        self.put(y, x, ("-" if self.ascii else "─") * max(0, width), style)

    def box(self, y, x, width, height, title="", style=DIM):
        if width < 2 or height < 2:
            return
        tl, tr, bl, br, vert = ("+", "+", "+", "+", "|") if self.ascii else ("┌", "┐", "└", "┘", "│")
        self.rule(y, x + 1, width - 2, style)
        self.rule(y + height - 1, x + 1, width - 2, style)
        for yy in range(y + 1, y + height - 1):
            self.put(yy, x, vert, style)
            self.put(yy, x + width - 1, vert, style)
        for yy, xx, char in ((y, x, tl), (y, x + width - 1, tr), (y + height - 1, x, bl), (y + height - 1, x + width - 1, br)):
            self.put(yy, xx, char, style)
        if title:
            self.put(y, x + 2, f" {title} "[:max(0, width - 4)], TITLE)

    def bar(self, y, x, width, value, maximum=100, style=GOOD):
        if width < 1:
            return
        full = "#" if self.ascii else "▰"
        empty = "." if self.ascii else "░"
        n = round(width * max(0, min(1, value / maximum))) if numeric(value) and numeric(maximum) and maximum > 0 else 0
        self.put(y, x, full * n, style)
        self.put(y, x + n, empty * (width - n), DIM)

    def spark(self, y, x, values, width, maximum, style=CYAN):
        if width <= 0:
            return
        levels = " .:-=+*#%@" if self.ascii else "▁▂▃▄▅▆▇█"
        vals = list(values)[-max(0, width):]
        vals = [None] * max(0, width - len(vals)) + vals
        for i, value in enumerate(vals):
            if numeric(value) and numeric(maximum) and maximum > 0:
                level = round(min(1, max(0, value / maximum)) * (len(levels) - 1))
                self.put(y, x + i, levels[level], style)
            else:
                self.put(y, x + i, "·" if not self.ascii else ".", DIM)

    def text(self):
        return "\n".join("".join(c for c, _ in row).rstrip() for row in self.cells).rstrip() + "\n"


def history(collector, key, span, width):
    return collector.history(key, seconds=span, width=max(1, width)) if collector else []


def gpu_panel(canvas, snapshot, collector, gpu, y, x, width, height, span):
    idx = gpu.get("index", "?")
    fresh = gpu.get("core_fresh") is True
    hbm_fresh = gpu.get("hbm_fresh", fresh) is True
    stats_fresh = gpu.get("stats_fresh") is True
    name = clean(gpu.get("name") or "NVIDIA GPU")
    canvas.box(y, x, width, height, f"GPU {idx}  {name[:max(0, width - 15)]}")
    inner = width - 4
    xx = x + 2
    core, hbm = gpu.get("core_c"), gpu.get("hbm_c")
    cutoffs = snapshot.get("cooling", {})
    core_cut = cutoffs.get("cutoff_core_c") or 80
    hbm_cut = cutoffs.get("cutoff_hbm_c") or 85
    core_style = temperature_style(core, core_cut) if fresh else DIM
    hbm_style = temperature_style(hbm, hbm_cut) if hbm_fresh else DIM
    half = inner // 2
    canvas.put(y + 1, xx, f"CORE {fmt(core, '°C')}{'' if fresh else '*'}", core_style)
    canvas.put(y + 1, xx + half, f"HBM {fmt(hbm, '°C')}{'' if hbm_fresh else '*'}", hbm_style)
    canvas.bar(y + 2, xx, max(1, half - 2), core, core_cut, core_style)
    canvas.bar(y + 2, xx + half, max(1, inner - half), hbm, hbm_cut, hbm_style)
    util = gpu.get("util_pct")
    power = gpu.get("power_w")
    limit = gpu.get("power_limit_w")
    canvas.put(y + 3, xx, f"LOAD {fmt(util, '%')}{'' if stats_fresh else '*'}", CYAN if stats_fresh else DIM)
    power_text = f"{fmt(power, 'W')}{'' if stats_fresh else '*'} / {fmt(limit, 'W')}"
    canvas.put(y + 3, xx + half, power_text, BLUE if stats_fresh else DIM)
    canvas.bar(y + 4, xx, max(1, half - 2), util, 100, CYAN if stats_fresh else DIM)
    canvas.bar(y + 4, xx + half, max(1, inner - half), power, limit or 300, BLUE if stats_fresh else DIM)
    used, total = gpu.get("memory_used_mb"), gpu.get("memory_total_mb")
    memory_text = (f"VRAM {used / 1024:.1f} / {total / 1024:.1f} GiB" if numeric(used) and numeric(total) else "VRAM -- (GPU details not available)")
    if not gpu.get("details_fresh"):
        memory_text = "* " + memory_text
    canvas.put(y + 5, xx, memory_text[:inner], NORMAL if gpu.get("details_fresh") else DIM)
    if height >= 12:
        graph_width = max(1, inner - 5)
        canvas.put(y + 6, xx, "CORE", core_style)
        canvas.spark(y + 6, xx + 5, history(collector, f"gpu.{idx}.core_c", span, graph_width), graph_width, core_cut, core_style)
        canvas.put(y + 7, xx, "HBM ", hbm_style)
        canvas.spark(y + 7, xx + 5, history(collector, f"gpu.{idx}.hbm_c", span, graph_width), graph_width, hbm_cut, hbm_style)
        canvas.put(y + 8, xx, "LOAD", CYAN)
        canvas.spark(y + 8, xx + 5, history(collector, f"gpu.{idx}.util_pct", span, graph_width), graph_width, 100, CYAN)
        canvas.put(y + 9, xx, "WATT", BLUE)
        canvas.spark(y + 9, xx + 5, history(collector, f"gpu.{idx}.power_w", span, graph_width), graph_width, limit or 300, BLUE)
        clocks = f"SM {fmt(gpu.get('sm_clock_mhz'), 'MHz')} · HBM {fmt(gpu.get('memory_clock_mhz'), 'MHz')}"
        if not gpu.get("details_fresh"):
            clocks += " *"
        canvas.put(y + 10, xx, clocks[:inner], NORMAL if gpu.get("details_fresh") else DIM)
        if height >= 13:
            canvas.put(y + 11, xx, f"T {age(gpu.get('core_age_seconds'))} · load {age(gpu.get('stats_age_seconds'))}"[:inner], DIM if fresh and stats_fresh else WARN)
    else:
        graph_width = max(1, inner - 5)
        canvas.put(y + 6, xx, "LOAD", CYAN)
        canvas.spark(y + 6, xx + 5, history(collector, f"gpu.{idx}.util_pct", span, graph_width), graph_width, 100, CYAN)
        canvas.put(y + 7, xx, f"{'fresh' if fresh else 'STALE'} · {age(gpu.get('core_age_seconds'))}"[:inner], DIM if fresh else WARN)


def draw_fans(canvas, snapshot, collector, y, x, width, height, span, compact=False):
    canvas.box(y, x, width, height, "CHASSIS FANS · measured")
    xx, inner = x + 2, width - 4
    fans = snapshot.get("fans", [])
    canvas.put(y + 1, xx, "FAN    RPM   PWM      PWM / HISTORY"[:inner], DIM)
    max_rows = height - 5 if height >= 11 else height - 3
    for i, fan in enumerate(fans[:max_rows]):
        yy = y + 2 + i
        fresh = fan.get("fresh") is True
        healthy = fan.get("healthy")
        style = BAD if healthy is False else GOOD if fresh else WARN
        fid = fan.get("fan_id", "?")
        canvas.put(yy, xx, f" {fid!s:<2} {fmt(fan.get('rpm')):>6} {fmt(fan.get('pwm_pct'), '%'):>4}{' ' if fresh else '*'}", style)
        available = max(0, inner - 19)
        bw = min(12, max(3, available // 3))
        canvas.bar(yy, xx + 19, bw, fan.get("pwm_pct"), 100, style)
        hw = max(0, available - bw - 1)
        canvas.spark(yy, xx + 20 + bw, history(collector, f"fan.{fid}.pwm_pct", span, hw), hw, 100, style)
    if not fans:
        canvas.put(y + 2, xx, "No chassis readings available"[:inner], WARN)
    if height >= 11:
        pwm = [f.get("pwm_pct") for f in fans if numeric(f.get("pwm_pct"))]
        min_pwm, max_pwm = (min(pwm), max(pwm)) if pwm else (None, None)
        info = f"Actual PWM {fmt(min_pwm, '%')}–{fmt(max_pwm, '%')} · request {fmt(snapshot.get('cooling', {}).get('requested_floor_pct'), '%')}"
        canvas.put(y + height - 3, xx, info[:inner], DIM)
        canvas.put(y + height - 2, xx, ("RPM verified; airflow not verified" if snapshot.get("cooling", {}).get("fan_rpm_verified") else "RPM uncalibrated; airflow not verified")[:inner], DIM)


def draw_cooling(canvas, snapshot, y, x, width, height):
    canvas.box(y, x, width, height, "COOLING / PROTECTION")
    cool = snapshot.get("cooling", {})
    xx, inner = x + 2, width - 4
    guard = snapshot.get("guard", {})
    guard_ok = guard.get("fresh") is True and cool.get("guard_healthy") is True
    fault, degraded = cool.get("fault_latched"), cool.get("control_degraded")
    status_fresh = snapshot.get("status", {}).get("fresh") is True
    verified = cool.get("controller_running") is True and cool.get("control_verified") is True and cool.get("setting_fresh") is True
    mode = "FAULT LATCHED" if fault else "STALE CONTROLLER STATUS" if not status_fresh else "DEGRADED" if degraded else "DYNAMIC CONTROL" if verified else "CONTROL UNCONFIRMED"
    canvas.put(y + 1, xx, mode[:inner], BAD if fault else GOOD if verified and status_fresh and not degraded else WARN)
    canvas.put(y + 2, xx, f"Guard {'HEALTHY' if guard_ok else 'UNCONFIRMED'} · {age(guard.get('age_seconds'))}"[:inner], GOOD if guard_ok else WARN)
    route = cool.get("control_path") or "unknown"
    canvas.put(y + 3, xx, f"iDRAC route {route} · {'verified' if cool.get('control_verified') else 'unverified'}"[:inner], NORMAL if cool.get("setting_fresh") else WARN)
    canvas.put(y + 4, xx, f"Floor {fmt(cool.get('requested_floor_pct'), '%')} · idle {fmt(cool.get('idle_min_pct'), '%')}–{fmt(cool.get('idle_max_pct'), '%')}"[:inner], NORMAL)
    if height >= 11:
        canvas.put(y + 5, xx, f"Thermal cutoffs core {fmt(cool.get('cutoff_core_c'), 'C')} / HBM {fmt(cool.get('cutoff_hbm_c'), 'C')}"[:inner], DIM)
        canvas.rule(y + 6, xx, inner)
        canvas.put(y + 7, xx, "CHASSIS TEMPERATURES", TITLE)
        temps = snapshot.get("temperatures", [])
        labels = []
        for t in temps:
            label = str(t.get("kind", "sensor")).upper()
            if t.get("kind") == "cpu":
                label += str(t.get("index") or "")
            labels.append(f"{label} {fmt(t.get('temperature_c'), 'C')}{'' if t.get('fresh') else '*'}")
        canvas.put(y + 8, xx, "   ".join(labels[:2])[:inner], NORMAL)
        canvas.put(y + 9, xx, "   ".join(labels[2:])[:inner], NORMAL)
    else:
        temps = snapshot.get("temperatures", [])
        labels = [f"{str(t.get('kind','?'))[:3].upper()}{t.get('index') or ''} {fmt(t.get('temperature_c'), 'C')}{'' if t.get('fresh') else '*'}" for t in temps]
        for line in range(min(2, max(0, height - 7))):
            canvas.put(y + 5 + line, xx, "  ".join(labels[line * 2:line * 2 + 2])[:inner], NORMAL)


def render_dashboard(snapshot, collector=None, *, width=120, height=40, span=300, ascii_only=False, paused=False):
    c = Canvas(width, height, ascii_only)
    if width < 70 or height < 23:
        c.put(0, 0, "cooltop · READ ONLY", TITLE)
        c.put(2, 0, f"Terminal {width}x{height}; resize to at least 70x23.", WARN)
        c.put(4, 0, "q quit   h help   --snapshot for plain output", DIM)
        return c
    system = snapshot.get("system", {})
    c.put(0, 1, "cooltop", TITLE)
    c.put(0, 10, "PAUSED" if paused else "● LIVE", WARN if paused else GOOD)
    c.put(0, 20, "READ ONLY · NVIDIA + iDRAC", DIM)
    timestamp = snapshot.get("sampled_unix")
    stamp = dt.datetime.fromtimestamp(timestamp).astimezone().strftime("%H:%M:%S %Z") if numeric(timestamp) else "--"
    c.put(0, max(48, width - len(stamp) - 2), stamp, DIM)
    host = clean(system.get("hostname") or "localhost")
    host_line = f"{host[:24]}   up {duration(system.get('uptime_s'))}   load {fmt(system.get('load1'), digits=2)} {fmt(system.get('load5'), digits=2)} {fmt(system.get('load15'), digits=2)}"
    if width >= 100 and numeric(system.get("cpu_count")):
        host_line += f" · {system['cpu_count']} CPUs"
    c.put(1, 1, host_line[:width - 2], DIM)
    cpu, mem = system.get("cpu_pct"), system.get("mem_pct")
    half = width // 2
    c.put(2, 1, f"CPU {fmt(cpu, '%'):>5}", CYAN)
    c.bar(2, 12, max(1, half - 15), cpu, 100, CYAN)
    used, total = system.get("mem_used_mb"), system.get("mem_total_mb")
    mem_label = f"RAM {fmt(mem, '%')}"
    if numeric(used) and numeric(total):
        mem_label += f"  {used / 1024:.1f}/{total / 1024:.1f} GiB"
    if width >= 100 and numeric(system.get("swap_total_mb")):
        mem_label += f" · swap {fmt((system.get('swap_used_mb') or 0) / 1024, digits=1)}/{system['swap_total_mb'] / 1024:.1f}G"
    c.put(2, half + 1, mem_label[:half - 3], BLUE)
    if height >= 29:
        c.bar(3, half + 1, half - 3, mem, 100, BLUE)
    if height >= 29:
        c.spark(3, 12, history(collector, "system.cpu_pct", span, max(1, half - 15)), max(1, half - 15), 100, CYAN)
    full = height >= 32
    gpu_height = 13 if full else 9
    gpu_y = 5 if full else 4 if height >= 29 else 3
    left_width = (width - 3) // 2
    right_width = width - 3 - left_width
    gpus = snapshot.get("gpus", [])[:2]
    for i in range(2):
        gpu = gpus[i] if i < len(gpus) else {"index": i, "name": "No telemetry"}
        gpu_panel(c, snapshot, collector, gpu, gpu_y, 1 if i == 0 else 2 + left_width,
                  left_width if i == 0 else right_width, gpu_height, span)
    bottom_y = gpu_y + gpu_height
    bottom_height = 11 if full or height >= 29 else 9
    draw_fans(c, snapshot, collector, bottom_y, 1, left_width, bottom_height, span, compact=not full)
    draw_cooling(c, snapshot, bottom_y, 2 + left_width, right_width, bottom_height)
    fy = bottom_y + bottom_height
    status = snapshot.get("status", {})
    cool = snapshot.get("cooling", {})
    status_state = status.get("state") or ("fresh" if status.get("fresh") else "unavailable")
    c.put(fy, 2, f"STATUS {status_state} · {age(status.get('age_seconds'))}   CHASSIS {age(cool.get('physical_age_seconds'))}", DIM if status.get("fresh") else WARN)
    reason = cool.get("reason") or cool.get("error") or status.get("error") or "Waiting for telemetry"
    if fy + 1 < height - 1:
        c.put(fy + 1, 2, f"Reason: {reason}"[:width - 4], WARN if cool.get("error") else NORMAL)
    if height > fy + 4:
        c.put(fy + 3, 2, f"History: last {span // 60}m · starts when cooltop opens · gaps = no fresh sample", DIM)
        c.put(fy + 4, 2, "Bars: temp/cutoff, load/100%, W/cap, PWM/100% · * = stale/unavailable", DIM)
    if height - 2 > fy + 1:
        c.rule(height - 2, 1, width - 2)
    c.put(height - 1, 2, f"q quit  p pause  h help  +/- history [{span // 60}m]  r refresh", DIM)
    if len(snapshot.get("gpus", [])) > 2:
        c.put(height - 1, width - 24, "first 2 GPUs displayed", WARN)
    return c


def help_overlay(canvas):
    lines = [
        "cooltop · keyboard & readings", "",
        "q / Esc       Quit", "p / Space     Pause / resume display", "h / ?         Close this help",
        "+ / -         History window: 1, 5, 15 minutes", "r             Refresh local telemetry", "",
        "The dashboard never changes fans, power, or inference.",
        "Core/HBM prefer the independent guard; watchdog is the fallback.",
        "Fan PWM/RPM and inlet/exhaust/CPU use cached iDRAC readings.",
        "A stale reading stays visible but is marked stale (* on sensors).",
        "Gaps in histories mean missing/stale data, not zero load.",
        "History begins when cooltop starts; no earlier samples invented.",
        "A requested fan floor is distinct from measured PWM/RPM.",
        "Spinning fans do not by themselves prove sufficient airflow.", "",
        "NVIDIA details: bounded queries for VRAM/name/power cap every 5s.",
        "--no-gpu-query disables these; no BMC polling occurs either way.",
        "--snapshot prints one plain dashboard without entering curses.",
    ]
    width = min(canvas.width - 4, 76)
    height = min(canvas.height - 2, len(lines) + 4)
    x, y = (canvas.width - width) // 2, (canvas.height - height) // 2
    for yy in range(y, y + height):
        canvas.put(yy, x, " " * width)
    canvas.box(y, x, width, height, "HELP", CYAN)
    for i, line in enumerate(lines[:height - 3]):
        canvas.put(y + 1 + i, x + 2, line[:width - 4], TITLE if i == 0 else NORMAL)


def draw_curses(screen, canvas):
    import curses
    screen.erase()
    for y, row in enumerate(canvas.cells):
        start = 0
        while start < len(row):
            style = row[start][1]
            end = start + 1
            while end < len(row) and row[end][1] == style:
                end += 1
            text = "".join(char for char, _ in row[start:end])
            attrs = curses.color_pair(style + 1) if curses.has_colors() else 0
            if style == TITLE:
                attrs |= curses.A_BOLD
            if style == DIM:
                attrs |= curses.A_DIM
            try:
                screen.addstr(y, start, text, attrs)
            except curses.error:
                pass  # Includes the lower-right cell and concurrent resizing.
            start = end
    screen.noutrefresh()
    curses.doupdate()


def run_curses(screen, collector, args):
    import curses
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
            background = -1
        except curses.error:
            background = curses.COLOR_BLACK
        colors = [curses.COLOR_WHITE, curses.COLOR_WHITE, curses.COLOR_CYAN, curses.COLOR_GREEN,
                  curses.COLOR_YELLOW, curses.COLOR_RED, curses.COLOR_CYAN, curses.COLOR_BLUE]
        for i, color in enumerate(colors, 1):
            curses.init_pair(i, color, background)
    screen.keypad(True)
    screen.timeout(100)
    span, paused, show_help = args.history, False, False
    snapshot = collector.sample()
    deadline = time.monotonic() + args.interval
    needs_redraw, last_size = True, None
    while True:
        now = time.monotonic()
        if not paused and now >= deadline:
            snapshot = collector.sample()
            deadline = now + args.interval
            needs_redraw = True
        height, width = screen.getmaxyx()
        if needs_redraw or last_size != (height, width):
            canvas = render_dashboard(snapshot, collector, width=width, height=height,
                                      span=span, ascii_only=args.ascii, paused=paused)
            if show_help:
                help_overlay(canvas)
            draw_curses(screen, canvas)
            needs_redraw, last_size = False, (height, width)
        key = screen.getch()
        if key != -1:
            needs_redraw = True
        if key in (ord("q"), ord("Q"), 27):
            return
        if key in (ord("h"), ord("?")):
            show_help = not show_help
        elif key in (ord("p"), ord("P"), ord(" ")):
            paused = not paused
            if not paused:
                deadline = 0
        elif key in (ord("+"), ord("="), ord("-"), ord("_")):
            current = min(range(len(SPANS)), key=lambda i: abs(SPANS[i] - span))
            current = max(0, min(len(SPANS) - 1, current + (1 if key in (ord("+"), ord("=")) else -1)))
            span = SPANS[current]
        elif key in (ord("r"), ord("R")) and not paused:
            deadline = 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"cooltop {VERSION}")
    parser.add_argument("--status-file", default=DEFAULT_STATUS, help="Existing watchdog JSON status file")
    parser.add_argument("--guard-file", default=DEFAULT_GUARD, help="Existing independent guard JSON status file")
    parser.add_argument("--gpu-details", dest="gpu_details", action="store_true", default=True, help="Enable bounded NVIDIA details queries (default; no BMC queries)")
    parser.add_argument("--no-gpu-query", dest="gpu_details", action="store_false", help="Read cached telemetry only; disable NVIDIA details queries")
    parser.add_argument("--interval", type=float, default=1.0, metavar="SECONDS", help="Refresh interval, 0.25–60 seconds (default: 1)")
    parser.add_argument("--history", type=int, default=300, choices=SPANS, metavar="SECONDS", help="History display window: 60, 300 or 900 seconds")
    parser.add_argument("--ascii", action="store_true", help="Use ASCII bars for terminals without Unicode")
    parser.add_argument("--snapshot", action="store_true", help="Print one plain dashboard and exit")
    parser.add_argument("--width", type=int, default=120, help="Snapshot width (70–240, default: 120)")
    parser.add_argument("--height", type=int, default=36, help="Snapshot height (23–100, default: 36)")
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval) or not 0.25 <= args.interval <= 60:
        parser.error("--interval must be between 0.25 and 60 seconds")
    if not 70 <= args.width <= 240 or not 23 <= args.height <= 100:
        parser.error("--width must be 70–240 and --height must be 23–100")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    if "UTF" not in (sys.stdout.encoding or "").upper():
        args.ascii = True
    collector = Collector(status_path=Path(args.status_file), guard_path=Path(args.guard_file),
                          gpu_details=args.gpu_details, history_seconds=900)
    try:
        if args.snapshot:
            snapshot = collector.sample()
            # The first optional GPU query is asynchronous; snapshots are bounded.
            if args.gpu_details:
                warmup_deadline = time.monotonic() + 2.5
                while time.monotonic() < warmup_deadline:
                    time.sleep(0.1)
                    snapshot = collector.sample()
                    if any(gpu.get("details_fresh") for gpu in snapshot.get("gpus", [])) or snapshot.get("gpu_details_error") not in (None, "starting"):
                        break
            print(render_dashboard(snapshot, collector, width=args.width, height=args.height,
                                   span=args.history, ascii_only=args.ascii).text(), end="")
            return 0
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            print("cooltop needs a terminal; use --snapshot for plain output.", file=sys.stderr)
            return 2
        import curses
        try:
            curses.wrapper(run_curses, collector, args)
        except curses.error as exc:
            print(f"cooltop: terminal initialization failed: {clean(exc)}", file=sys.stderr)
            return 1
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError) as exc:
        print(f"cooltop: {clean(exc)}", file=sys.stderr)
        return 1
    finally:
        close = getattr(collector, "close", None)
        if close:
            close()


if __name__ == "__main__":
    raise SystemExit(main())
