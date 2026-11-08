"""Read-only local telemetry for cooltop. Never contacts a BMC or controls hardware."""
from __future__ import annotations

from collections import deque
import csv
import datetime as dt
import io
import json
import math
import os
from pathlib import Path
import socket
import stat
import subprocess
import threading
import time

STATUS_PATH = Path('/var/lib/airc-gpu-fan-watchdog/status.json')
GUARD_PATH = Path('/run/airc-gpu-thermal-guard/guard.json')


def num(value, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value):
            return None
    except OverflowError:
        return None
    return value if (low is None or value >= low) and (high is None or value <= high) else None


def clean(value, limit=180):
    return ''.join(c for c in str(value or '') if c.isprintable())[:limit]


def obj(value):
    return value if isinstance(value, dict) else {}


def rows(value, maximum=64):
    return [v for v in value[:maximum] if isinstance(v, dict)] if isinstance(value, list) else []


def boolean(value):
    return value if type(value) is bool else None


def stamp(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def read_json(path):
    """Bound the read and refuse devices, FIFOs, symlinks, and ambiguous JSON."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd) as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None, 'invalid'
            text = stream.read(1024 * 1024 + 1)
        if len(text) > 1024 * 1024:
            return None, 'invalid'
        value = json.loads(text, object_pairs_hook=unique)
        return (value, None) if isinstance(value, dict) else (None, 'invalid')
    except FileNotFoundError:
        return None, 'missing'
    except PermissionError:
        return None, 'permission'
    except (OSError, ValueError, UnicodeError, RecursionError, OverflowError):
        return None, 'invalid'


def source_state(raw, boot, mono, wall, ttl=90, error=None):
    if error or not isinstance(raw, dict):
        return dict(state=error or 'missing', fresh=False, age_seconds=None, error=error or 'missing')
    clock = num(raw.get('monotonic'), 0)
    if boot and raw.get('boot_id') != boot:
        return dict(state='previous-boot', fresh=False, age_seconds=None, error='boot mismatch')
    if boot and clock is None:
        return dict(state='invalid', fresh=False, age_seconds=None, error='missing monotonic clock')
    observed = stamp(raw.get('utc'))
    age = mono - clock if clock is not None and boot else wall - observed if observed is not None else None
    state = 'unknown-clock' if age is None else 'future' if age < 0 else 'fresh' if age <= ttl else 'stale'
    return dict(state=state, fresh=state == 'fresh', age_seconds=age, error=None)


def subsource(raw, status, wall, ttl):
    age_at = num(raw.get('age_seconds'), 0)
    status_age = status['age_seconds']
    observed = num(raw.get('observed_time_unix'), 0)
    age = (status_age + age_at if age_at is not None and status_age is not None
           else wall - observed if observed is not None else None)
    maximum = num(raw.get('max_age_seconds'), 1, 3600) or ttl
    fresh = bool(status['fresh'] and raw.get('fresh') is True and age is not None and 0 <= age <= min(ttl, maximum))
    return fresh, age


def normalize(raw, guard, *, boot, mono, wall, status_error=None, guard_error=None):
    raw, guard = obj(raw), obj(guard)
    if not status_error and not all(key in raw for key in ('utc', 'monotonic', 'gpus', 'requested_floor_pct')):
        status_error = 'invalid'
    if not guard_error and not all(key in guard for key in ('monotonic', 'gpus', 'healthy')):
        guard_error = 'invalid'
    status = source_state(raw, boot, mono, wall, error=status_error)
    guard_state = source_state(guard, boot, mono, wall, ttl=10, error=guard_error)
    diagnostics = obj(raw.get('cooling_diagnostics'))
    physical = obj(diagnostics.get('physical_diagnostics'))
    setting = obj(diagnostics.get('setting_proof'))
    physical_fresh, physical_age = subsource(physical, status, wall, 90)
    setting_fresh, setting_age = subsource(setting, status, wall, 90)
    cutoffs = obj(diagnostics.get('configured_cutoffs_c'))
    idle = obj(raw.get('idle_control'))
    idle_config = obj(idle.get('config'))
    cooling = {
        'requested_floor_pct': num(raw.get('requested_floor_pct'), 0, 100),
        'reason': clean(raw.get('reason')), 'control_path': clean(raw.get('control_path'), 32),
        'controller_running': boolean(raw.get('controller_running')),
        'control_verified': boolean(setting.get('verified')) if setting_fresh else None,
        'control_degraded': boolean(raw.get('control_degraded')),
        'fault_latched': boolean(raw.get('fault_latched')), 'error': clean(raw.get('error')),
        'setting_fresh': setting_fresh, 'setting_age_seconds': setting_age,
        'physical_fresh': physical_fresh, 'physical_age_seconds': physical_age,
        'fan_pwm_verified': boolean(physical.get('fan_pwm_verified')) if physical_fresh else None,
        'fan_rpm_verified': boolean(physical.get('fan_rpm_verified')) if physical_fresh else None,
        'cutoff_core_c': num(cutoffs.get('core'), 1, 150), 'cutoff_hbm_c': num(cutoffs.get('hbm'), 1, 150),
        'guard_healthy': boolean(guard.get('healthy')) if guard_state['fresh'] else None,
        'idle_min_pct': num(idle_config.get('min_pct'), 0, 100),
        'idle_max_pct': num(idle_config.get('max_pct'), 0, 100),
        'idle_current_pct': num(idle.get('current_pct'), 0, 100),
        'airflow_proven': boolean(diagnostics.get('airflow_proven')),
    }
    gpu_diags = {r.get('gpu'): r for r in rows(diagnostics.get('gpu_diagnostics')) if type(r.get('gpu')) is int}
    guarded = {r.get('gpu'): r for r in rows(guard.get('gpus')) if type(r.get('gpu')) is int}
    gpus = []
    seen = set()
    for row in rows(raw.get('gpus')) + rows(guard.get('gpus')):
        index = row.get('gpu')
        if type(index) is not int or not 0 <= index <= 255 or index in seen:
            continue
        seen.add(index)
        rawrow = next((r for r in rows(raw.get('gpus')) if type(r.get('gpu')) is int and r.get('gpu') == index), {})
        diagnostic = gpu_diags.get(index, {})
        age_at = num(diagnostic.get('age_seconds'), 0) or 0
        age = status['age_seconds'] + age_at if status['age_seconds'] is not None else None
        stats_fresh = bool(status['fresh'] and age is not None and 0 <= age <= 10 and diagnostic.get('fresh', True) is True)
        use_guard = guard_state['fresh'] and index in guarded
        temp = guarded[index] if use_guard else rawrow
        gpus.append(dict(index=index, core_c=num(temp.get('core_c'), 0, 140), hbm_c=num(temp.get('hbm_c'), 0, 150),
                         util_pct=num(rawrow.get('util_pct'), 0, 100), power_w=num(rawrow.get('power_w'), 0, 2000),
                         name=None, memory_used_mb=None, memory_total_mb=None, power_limit_w=None,
                         sm_clock_mhz=None, memory_clock_mhz=None,
                         core_fresh=bool(use_guard or stats_fresh), hbm_fresh=bool(use_guard or stats_fresh),
                         core_age_seconds=guard_state['age_seconds'] if use_guard else age,
                         core_source='guard' if use_guard else 'watchdog',
                         stats_fresh=stats_fresh, stats_age_seconds=age, stats_source='watchdog',
                         details_fresh=False, details_age_seconds=None))
    fans = []
    for row in rows(physical.get('fans'), 256):
        index = row.get('fan_id')
        if type(index) is not int or not 1 <= index <= 256 or any(f['fan_id'] == index for f in fans):
            continue
        fans.append(dict(fan_id=index, rpm=num(row.get('rpm'), 0, 100000), pwm_pct=num(row.get('pwm_pct'), 0, 100),
                         healthy=str(row.get('health', '')).lower() == 'ok' if 'health' in row else None,
                         fresh=physical_fresh, age_seconds=physical_age))
    temperatures = []
    for row in rows(physical.get('temperatures'), 256):
        kind, index = row.get('kind'), row.get('index')
        index = 0 if index is None and kind in ('inlet', 'exhaust') else index
        if kind not in ('inlet', 'exhaust', 'cpu') or type(index) is not int or not 0 <= index <= 256:
            continue
        temperatures.append(dict(kind=kind, index=index, temperature_c=num(row.get('temperature_c'), -40, 150),
                                 fresh=physical_fresh and row.get('available', True) is True, age_seconds=physical_age))
    return dict(sampled_unix=wall, sample_monotonic=mono, status=status, guard=guard_state,
                cooling=cooling, gpus=sorted(gpus, key=lambda r: r['index']), fans=sorted(fans, key=lambda r: r['fan_id']),
                temperatures=temperatures)


class History:
    """Actual sample events only; redraws and repeated cached observations add nothing."""
    def __init__(self, seconds=600, max_samples=10000):
        self.seconds, self.max_samples = max(10, seconds), max(10, max_samples)
        self.values, self.tokens = {}, {}

    def add(self, key, observed, value, token, now):
        if num(value) is None or num(observed) is None or observed > now or observed < now - self.seconds:
            return
        series = self.values.setdefault(key, deque(maxlen=self.max_samples))
        if (key in self.tokens and self.tokens[key] == token) or (series and observed <= series[-1][0]):
            return
        self.tokens[key] = token
        series.append((observed, value))
        while series and series[0][0] < now - self.seconds:
            series.popleft()

    def bins(self, key, now, seconds=300, width=60):
        width = max(1, min(1000, int(width)))
        seconds = max(1, min(float(seconds), self.seconds))
        result = [None] * width
        for observed, value in self.values.get(key, ()):
            if now - seconds <= observed <= now:
                position = min(width - 1, int((observed - (now - seconds)) / seconds * width))
                result[position] = value
        return result


class GpuDetails:
    """Optional low-rate, bounded nvidia-smi read; never blocks the drawing thread."""
    def __init__(self):
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._result = (None, {}, 'starting')
        self._thread = threading.Thread(target=self._run, name='cooltop-gpu-details', daemon=True)
        self._thread.start()

    def _run(self):
        query = 'index,name,memory.used,memory.total,power.limit,clocks.current.sm,clocks.current.memory,power.draw,utilization.gpu,temperature.gpu'
        while not self._stop.is_set():
            try:
                output = subprocess.run(['nvidia-smi', '--query-gpu=' + query, '--format=csv,noheader,nounits'],
                                        text=True, capture_output=True, timeout=2, check=True)
                if len(output.stdout) > 65536:
                    raise ValueError('oversize query response')
                parsed = {}
                for row in list(csv.reader(io.StringIO(output.stdout)))[:64]:
                    if len(row) != 10:
                        continue
                    index = int(row[0].strip())
                    if not 0 <= index <= 255 or index in parsed:
                        continue
                    values = []
                    for cell in row[2:]:
                        try:
                            values.append(num(float(cell.strip()), 0, 1000000))
                        except ValueError:
                            values.append(None)
                    parsed[index] = dict(zip(('memory_used_mb', 'memory_total_mb', 'power_limit_w', 'sm_clock_mhz', 'memory_clock_mhz', 'power_w', 'util_pct', 'core_c'), values))
                    parsed[index]['name'] = clean(row[1].strip(), 64)
                state = (time.monotonic(), parsed, None if parsed else 'no GPU details')
            except (OSError, ValueError, subprocess.SubprocessError):
                state = (None, {}, 'GPU details unavailable')
            with self._lock:
                self._result = state
            self._stop.wait(5)

    def read(self):
        with self._lock:
            return self._result

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2.2)


class Collector:
    def __init__(self, status_path=STATUS_PATH, guard_path=GUARD_PATH, gpu_details=False, history_seconds=600, proc_root='/proc'):
        self.status_path, self.guard_path = Path(status_path), Path(guard_path)
        self.proc = Path(proc_root)
        self._history = History(history_seconds)
        self._last_cpu = None
        self._now = time.monotonic()
        self.details = GpuDetails() if gpu_details else None
        try:
            self.boot = (self.proc / 'sys/kernel/random/boot_id').read_text().strip()
        except OSError:
            self.boot = None

    def _system(self):
        result = dict(hostname=clean(socket.gethostname(), 64), cpu_count=os.cpu_count(), cpu_pct=None,
                      mem_pct=None, mem_total_mb=None, mem_used_mb=None, swap_total_mb=None, swap_used_mb=None,
                      load1=None, load5=None, load15=None, uptime_s=None)
        try:
            cpu = [int(v) for v in (self.proc / 'stat').read_text().splitlines()[0].split()[1:9]]
            total, idle = sum(cpu), cpu[3] + cpu[4]
            if self._last_cpu:
                delta, idle_delta = total - self._last_cpu[0], idle - self._last_cpu[1]
                if delta > 0 and 0 <= idle_delta <= delta:
                    result['cpu_pct'] = 100 * (delta - idle_delta) / delta
            self._last_cpu = total, idle
        except (OSError, ValueError, IndexError):
            pass
        try:
            memory = {line.split(':')[0]: int(line.split()[1]) for line in (self.proc / 'meminfo').read_text().splitlines() if ':' in line}
            total, available = memory.get('MemTotal'), memory.get('MemAvailable')
            if total and available is not None and 0 <= available <= total:
                result.update(mem_pct=100 * (total - available) / total, mem_total_mb=total / 1024, mem_used_mb=(total - available) / 1024)
            swap_total, swap_free = memory.get('SwapTotal'), memory.get('SwapFree')
            if swap_total is not None and swap_free is not None and 0 <= swap_free <= swap_total:
                result.update(swap_total_mb=swap_total / 1024, swap_used_mb=(swap_total - swap_free) / 1024)
        except (OSError, ValueError, IndexError):
            pass
        try:
            values = (self.proc / 'loadavg').read_text().split()
            result.update(zip(('load1', 'load5', 'load15'), (num(float(v), 0) for v in values[:3])))
            result['uptime_s'] = num(float((self.proc / 'uptime').read_text().split()[0]), 0)
        except (OSError, ValueError, IndexError):
            pass
        return result

    def sample(self):
        wall, mono = time.time(), time.monotonic()
        self._now = mono
        raw, status_error = read_json(self.status_path)
        guard, guard_error = read_json(self.guard_path)
        snapshot = normalize(raw, guard, boot=self.boot, mono=mono, wall=wall, status_error=status_error, guard_error=guard_error)
        snapshot['system'] = self._system()
        snapshot['gpu_details_enabled'] = self.details is not None
        detail_time, detail_rows, detail_error = self.details.read() if self.details else (None, {}, 'disabled')
        snapshot['gpu_details_error'] = detail_error
        detail_age = mono - detail_time if detail_time is not None else None
        if detail_age is not None and 0 <= detail_age <= 15:
            known_indices = {gpu['index'] for gpu in snapshot['gpus']}
            for index in sorted(detail_rows):
                if type(index) is int and 0 <= index <= 255 and index not in known_indices:
                    snapshot['gpus'].append(dict(index=index, core_c=None, hbm_c=None, util_pct=None, power_w=None,
                                                name=None, memory_used_mb=None, memory_total_mb=None, power_limit_w=None,
                                                sm_clock_mhz=None, memory_clock_mhz=None, core_fresh=False, hbm_fresh=False,
                                                core_age_seconds=None, core_source='unavailable', stats_fresh=False,
                                                stats_age_seconds=None, stats_source='unavailable', details_fresh=False, details_age_seconds=None))
            snapshot['gpus'].sort(key=lambda row: row['index'])
        for gpu in snapshot['gpus']:
            details_fresh = detail_age is not None and 0 <= detail_age <= 15
            if gpu['index'] in detail_rows:
                values = detail_rows[gpu['index']]
                gpu.update({key: value for key, value in values.items() if key not in ('power_w', 'util_pct', 'core_c')})
                if details_fresh and (not gpu['stats_fresh'] or detail_age < gpu['stats_age_seconds']):
                    gpu.update(power_w=num(values.get('power_w'), 0, 2000), util_pct=num(values.get('util_pct'), 0, 100),
                               stats_fresh=True, stats_age_seconds=detail_age, stats_source='nvidia-smi')
                detail_core = num(values.get('core_c'), 0, 140)
                if details_fresh and detail_core is not None and not gpu['core_fresh']:
                    gpu.update(core_c=detail_core, core_fresh=True, core_age_seconds=detail_age, core_source='nvidia-smi')
                    # HBM cannot be inferred from the core sensor. Drop an old HBM
                    # value rather than presenting it with the fresh core timestamp.
                    gpu.update(hbm_c=None, hbm_fresh=False)
            gpu.update(details_fresh=details_fresh and gpu['index'] in detail_rows, details_age_seconds=detail_age)
            prefix = f"gpu.{gpu['index']}."
            for field in ('core_c', 'hbm_c', 'power_w', 'util_pct', 'memory_used_mb'):
                fresh = gpu['details_fresh'] if field == 'memory_used_mb' else gpu['hbm_fresh'] if field == 'hbm_c' else gpu['core_fresh'] if field == 'core_c' else gpu['stats_fresh']
                age = detail_age if field == 'memory_used_mb' else gpu['core_age_seconds'] if field in ('core_c', 'hbm_c') else gpu['stats_age_seconds']
                observed = mono - age if age is not None else None
                if fresh:
                    self._history.add(prefix + field, observed, gpu.get(field), round(observed, 3), mono)
        for fan in snapshot['fans']:
            if fan['fresh']:
                observed = mono - fan['age_seconds']
                for field in ('rpm', 'pwm_pct'):
                    self._history.add(f"fan.{fan['fan_id']}.{field}", observed, fan[field], round(observed, 3), mono)
        for sensor in snapshot['temperatures']:
            if sensor['fresh']:
                observed = mono - sensor['age_seconds']
                self._history.add(f"temperature.{sensor['kind']}.{sensor['index']}", observed, sensor['temperature_c'], round(observed, 3), mono)
        if snapshot['status']['fresh']:
            self._history.add('cooling.requested_floor_pct', mono - snapshot['status']['age_seconds'], snapshot['cooling']['requested_floor_pct'], obj(raw).get('monotonic'), mono)
        for field in ('cpu_pct', 'mem_pct'):
            self._history.add('system.' + field, mono, snapshot['system'][field], mono, mono)
        return snapshot

    def history(self, key, seconds=300, width=60):
        return self._history.bins(key, self._now, seconds, width)

    def close(self):
        if self.details:
            self.details.close()
