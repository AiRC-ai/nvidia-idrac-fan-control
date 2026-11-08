#!/usr/bin/env python3
"""Independent, read-only monitoring of the NVIDIA/iDRAC guest controller."""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import platform
import re
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time

VERSION = "0.2.0"
MAX_JSON_BYTES = 64 * 1024
CLOCK_DOMAIN = "management-system-monotonic-v1"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
REMOTE_PATH = re.compile(r"/[A-Za-z0-9_./-]+")


class GuardError(Exception):
    """Bounded diagnostics: never include remote output or configuration values."""


class ConfigError(GuardError):
    pass


class HeartbeatError(GuardError):
    pass


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise GuardError("duplicate JSON key")
        value[key] = item
    return value


def decode_json(text: str):
    def invalid_constant(_value):
        raise GuardError("non-finite JSON value")
    try:
        return json.loads(text, object_pairs_hook=_object, parse_constant=invalid_constant)
    except (ValueError, TypeError, RecursionError) as exc:
        raise GuardError("invalid JSON") from exc


def number(value, low=0, high=1e15):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise GuardError("invalid numeric field")
    return value


def boolean(value):
    if type(value) is not bool:
        raise GuardError("invalid boolean field")
    return value


def boot_id(path=Path("/proc/sys/kernel/random/boot_id")) -> str:
    try:
        value = path.read_text().strip()
    except OSError as exc:
        raise GuardError("Linux boot identity unavailable") from exc
    if not UUID.fullmatch(value):
        raise GuardError("invalid Linux boot identity")
    return value


def management_boot_id(*, system=None, runner=None) -> str:
    """Read management-host boot identity; never expose it in public receipts."""
    system = platform.system() if system is None else system
    if system == "Linux":
        return boot_id()
    if system != "Darwin":
        raise GuardError("management host must be Linux or macOS")
    runner = subprocess.run if runner is None else runner
    try:
        result = runner(["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
                        stdin=subprocess.DEVNULL, capture_output=True, text=True,
                        timeout=5, check=False)
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise GuardError("management boot identity unavailable") from exc
    if result.returncode != 0 or len(result.stdout) > 512:
        raise GuardError("management boot identity unavailable")
    value = result.stdout.strip().lower()
    if not UUID.fullmatch(value):
        raise GuardError("invalid management boot identity")
    return value


def management_monotonic(*, system=None) -> float:
    """Same-boot, cross-process clock; macOS Python 3.9 monotonic is relative."""
    system = platform.system() if system is None else system
    try:
        if system == "Darwin":
            # Python 3.9's macOS time.monotonic() can have a process-specific
            # origin. CLOCK_MONOTONIC keeps persisted durations comparable.
            value = time.clock_gettime(time.CLOCK_MONOTONIC)
        elif system == "Linux":
            value = time.monotonic()
        else:
            raise GuardError("management host must be Linux or macOS")
        return number(value)
    except (AttributeError, OSError, ValueError, OverflowError) as exc:
        raise GuardError("management monotonic clock unavailable") from exc


def private_read(path: Path) -> str:
    """Read one bounded regular file without following a final-component symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_uid not in (0, os.geteuid()):
                raise GuardError("input file must have a trusted owner and no untrusted write access")
            if info.st_size > MAX_JSON_BYTES:
                raise GuardError("input file too large")
            text = handle.read(MAX_JSON_BYTES + 1)
            if len(text.encode("utf-8")) > MAX_JSON_BYTES:
                raise GuardError("input file too large")
            return text
    except (OSError, UnicodeError) as exc:
        raise GuardError("input file unavailable") from exc


def production_status(raw: dict) -> dict:
    """Explicit adapter for the documented production cache; no shape guessing."""
    if not isinstance(raw, dict):
        raise HeartbeatError("production status must be an object")
    try:
        stamp = datetime.datetime.fromisoformat(raw["utc"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        raise HeartbeatError("production UTC field absent or malformed") from exc
    if stamp.tzinfo is None:
        raise HeartbeatError("production UTC field must be timezone aware")
    running = boolean(raw.get("controller_running"))
    fault = boolean(raw.get("fault_latched"))
    degraded = boolean(raw.get("control_degraded"))
    verified = boolean(raw.get("control_verified"))
    floor = raw.get("requested_floor_pct")
    if type(floor) is not int or not 1 <= floor <= 100:
        raise HeartbeatError("invalid production requested floor")
    instance = raw.get("invocation_id")
    if not isinstance(instance, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", instance):
        raise HeartbeatError("production invocation identity absent or malformed")
    gpus = raw.get("gpus")
    if not isinstance(gpus, list) or len(gpus) > 64:
        raise HeartbeatError("invalid production GPU list")
    indices = set()
    for gpu in gpus:
        if not isinstance(gpu, dict) or type(gpu.get("gpu")) is not int or not 0 <= gpu["gpu"] < 64 or gpu["gpu"] in indices:
            raise HeartbeatError("invalid production GPU identity")
        indices.add(gpu["gpu"])
        number(gpu.get("core_c"), 0, 130)
        number(gpu.get("hbm_c"), 0, 140)
        number(gpu.get("power_w"), 0, 2000)
        number(gpu.get("util_pct"), 0, 100)
    if not gpus and running and not fault and not degraded and verified:
        raise HeartbeatError("healthy production cache has no GPU samples")
    return {"schema": 1, "boot_id": raw.get("boot_id"),
            "sample_monotonic": raw.get("monotonic"), "cycle_sequence": raw.get("cycle_sequence"),
            "controller_running": running, "dry_run": False, "fault": fault,
            "control_degraded": degraded, "floor_setting_verified": verified,
            "control_verified": verified, "verified_floor_pct": floor if verified else None,
            "producer_instance": hashlib.sha256(instance.encode()).hexdigest()}


def heartbeat(path: Path, *, status_format="generic", current_boot=None, now=None) -> dict:
    """Guest-side helper: no subprocess, NVIDIA call, network request, or write."""
    current_boot = boot_id() if current_boot is None else current_boot
    now = time.monotonic() if now is None else now
    number(now)
    if not isinstance(current_boot, str) or not UUID.fullmatch(current_boot):
        raise HeartbeatError("invalid current boot identity")
    raw = decode_json(private_read(path))
    if status_format == "production":
        raw = production_status(raw)
    elif status_format != "generic":
        raise HeartbeatError("unsupported guest status format")
    if not isinstance(raw, dict) or type(raw.get("schema")) is not int or raw["schema"] != 1:
        raise HeartbeatError("unsupported guest status schema")
    if raw.get("boot_id") != current_boot:
        raise HeartbeatError("guest status is from another boot or lacks boot identity")
    sampled = number(raw.get("sample_monotonic"), 0, now)
    sequence = raw.get("cycle_sequence")
    if type(sequence) is not int or sequence < 1:
        raise HeartbeatError("guest status has no completed controller cycle")
    verified = boolean(raw.get("floor_setting_verified"))
    if "control_verified" in raw and boolean(raw["control_verified"]) != verified:
        raise HeartbeatError("inconsistent guest verification fields")
    floor = raw.get("verified_floor_pct")
    if verified and (type(floor) is not int or not 1 <= floor <= 100):
        raise HeartbeatError("invalid verified guest floor")
    instance = raw.get("producer_instance")
    if instance is not None and (not isinstance(instance, str) or not re.fullmatch(r"[a-f0-9]{64}", instance)):
        raise HeartbeatError("invalid producer instance fingerprint")
    return {"protocol": 1, "guest_boot_id": current_boot, "sample_monotonic": sampled,
            "helper_monotonic": now, "sample_age_seconds": now - sampled,
            "cycle_sequence": sequence, "controller_running": boolean(raw.get("controller_running")),
            "dry_run": boolean(raw.get("dry_run")), "fault": boolean(raw.get("fault")),
            "control_degraded": boolean(raw.get("control_degraded", False)), "producer_instance": instance,
            "floor_setting_verified": verified, "verified_floor_pct": floor if verified else None}


@dataclasses.dataclass(frozen=True)
class Config:
    guest_host: str
    guest_user: str
    identity_file: Path
    known_hosts_file: Path
    state_file: Path
    status_file: Path
    lock_file: Path
    remote_helper: str = "/usr/local/lib/nvidia-idrac-fan/external_guard.py"
    remote_status_file: str = "/var/lib/nvidia-idrac-fan/status.json"
    remote_python: str = "/usr/bin/python3"
    remote_status_format: str = "generic"
    ssh: str = "/usr/bin/ssh"
    ssh_port: int = 22
    interval_seconds: float = 10
    command_timeout_seconds: int = 15
    connect_timeout_seconds: int = 5
    stale_after_seconds: float = 120
    qualify_after_seconds: float = 30
    recovery_samples: int = 3


def load_config(path: Path) -> Config:
    try:
        raw = decode_json(private_read(path))
        if not isinstance(raw, dict):
            raise ConfigError("configuration must be an object")
        for name in ("identity_file", "known_hosts_file", "state_file", "status_file", "lock_file"):
            raw[name] = Path(raw[name]).expanduser()
            if not raw[name].is_absolute():
                raise ConfigError("local file paths must be absolute")
        c = Config(**raw)
    except (TypeError, KeyError, ValueError) as exc:
        raise ConfigError("invalid configuration keys or types") from exc
    if not isinstance(c.guest_host, str) or not isinstance(c.guest_user, str):
        raise ConfigError("guest host and user must be strings")
    try:
        ipaddress.ip_address(c.guest_host)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", c.guest_host):
            raise ConfigError("invalid guest host")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", c.guest_user):
        raise ConfigError("invalid guest user")
    if c.remote_status_format not in ("generic", "production"):
        raise ConfigError("invalid remote status format")
    for field, low, high in (("ssh_port", 1, 65535), ("command_timeout_seconds", 2, 60),
                             ("connect_timeout_seconds", 1, 30), ("recovery_samples", 1, 20)):
        value = getattr(c, field)
        if type(value) is not int or not low <= value <= high:
            raise ConfigError("invalid integer configuration")
    for field, low, high in (("interval_seconds", 1, 60), ("stale_after_seconds", 5, 3600),
                             ("qualify_after_seconds", 1, 3600)):
        number(getattr(c, field), low, high)
    for value in (c.remote_python, c.remote_helper, c.remote_status_file):
        if not isinstance(value, str) or not REMOTE_PATH.fullmatch(value) or ".." in Path(value).parts:
            raise ConfigError("remote paths must be absolute without shell metacharacters or parent traversal")
    if not isinstance(c.ssh, str) or not Path(c.ssh).is_absolute():
        raise ConfigError("SSH executable must be absolute")
    for value in (c.identity_file, c.known_hosts_file):
        if re.search(r"[\s%$\"'\\]", str(value)):
            raise ConfigError("SSH file paths must not contain expansion or filename-list characters")
    paths = [path, c.identity_file, c.known_hosts_file, c.state_file, c.status_file, c.lock_file, Path(c.ssh)]
    try:
        for i, first in enumerate(paths):
            for second in paths[i + 1:]:
                if first.resolve() == second.resolve() or first.exists() and second.exists() and first.samefile(second):
                    raise ConfigError("configuration, executable, SSH, state, status and lock paths must be distinct")
        for key, forbidden in ((c.identity_file, 0o077), (c.known_hosts_file, 0o022)):
            info = key.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & forbidden or info.st_uid not in (0, os.geteuid()) or not info.st_size:
                raise ConfigError("SSH files need trusted ownership, restrictive modes and content")
    except (OSError, RuntimeError) as exc:
        raise ConfigError("configured paths unavailable") from exc
    return c


def ssh_command(c: Config) -> list[str]:
    command = [c.ssh, "-F", "/dev/null", "-T", "-p", str(c.ssh_port), "-i", str(c.identity_file)]
    for option in ("BatchMode=yes", "PasswordAuthentication=no", "KbdInteractiveAuthentication=no",
                   "IdentitiesOnly=yes", "StrictHostKeyChecking=yes", "GlobalKnownHostsFile=/dev/null",
                   "UserKnownHostsFile=" + str(c.known_hosts_file), "ConnectionAttempts=1",
                   "ConnectTimeout=" + str(c.connect_timeout_seconds), "ServerAliveInterval=5",
                   "ServerAliveCountMax=1", "LogLevel=ERROR"):
        command.extend(["-o", option])
    # OpenSSH sends a remote shell command. Every remote word above/below has
    # deliberately restricted syntax; no configurable arbitrary command exists.
    return command + [c.guest_user + "@" + c.guest_host, c.remote_python, c.remote_helper,
                      "heartbeat", "--status-file", c.remote_status_file,
                      "--status-format", c.remote_status_format]


def fetch_heartbeat(c: Config, runner=subprocess.run) -> dict:
    try:
        result = runner(ssh_command(c), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                        timeout=c.command_timeout_seconds, check=False)
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise HeartbeatError("heartbeat SSH unavailable or timed out") from exc
    if result.returncode != 0:
        raise HeartbeatError("guest heartbeat unavailable")
    if len(result.stdout) > MAX_JSON_BYTES:
        raise HeartbeatError("heartbeat response too large")
    return validate_heartbeat(decode_json(result.stdout))


def validate_heartbeat(value) -> dict:
    if not isinstance(value, dict) or type(value.get("protocol")) is not int or value["protocol"] != 1:
        raise HeartbeatError("unsupported heartbeat protocol")
    if not isinstance(value.get("guest_boot_id"), str) or not UUID.fullmatch(value["guest_boot_id"]):
        raise HeartbeatError("heartbeat boot identity absent")
    sampled = number(value.get("sample_monotonic"))
    current = number(value.get("helper_monotonic"), sampled)
    age = number(value.get("sample_age_seconds"))
    if not math.isclose(current - sampled, age, rel_tol=1e-9, abs_tol=0.001):
        raise HeartbeatError("inconsistent heartbeat age")
    if type(value.get("cycle_sequence")) is not int or value["cycle_sequence"] < 1:
        raise HeartbeatError("heartbeat sequence absent")
    for name in ("controller_running", "dry_run", "fault", "floor_setting_verified"):
        boolean(value.get(name))
    boolean(value.get("control_degraded", False))
    instance = value.get("producer_instance")
    if instance is not None and (not isinstance(instance, str) or not re.fullmatch(r"[a-f0-9]{64}", instance)):
        raise HeartbeatError("invalid heartbeat producer instance")
    floor = value.get("verified_floor_pct")
    if value["floor_setting_verified"] and (type(floor) is not int or not 1 <= floor <= 100):
        raise HeartbeatError("invalid heartbeat floor")
    return value


@dataclasses.dataclass
class State:
    management_boot_id: str
    bad_since: float | None = None
    alert: bool = False
    recovery_count: int = 0
    previous_sample: str | None = None
    last_observation: float | None = None
    guest_boot_id: str | None = None
    producer_instance: str | None = None
    sample_monotonic: float | None = None
    cycle_sequence: int | None = None
    clock_domain: str = CLOCK_DOMAIN


def load_state(path: Path, current_boot: str) -> State:
    if not path.exists():
        return State(current_boot)
    raw = decode_json(private_read(path))
    try:
        s = State(**raw)
    except (TypeError, ValueError) as exc:
        raise GuardError("invalid external guard state") from exc
    if not isinstance(s.management_boot_id, str) or not UUID.fullmatch(s.management_boot_id):
        raise GuardError("invalid persisted boot identity")
    if not isinstance(s.clock_domain, str) or len(s.clock_domain) > 80:
        raise GuardError("invalid persisted clock domain")
    boolean(s.alert)
    if type(s.recovery_count) is not int or not 0 <= s.recovery_count <= 20:
        raise GuardError("invalid recovery counter")
    if s.previous_sample is not None and (not isinstance(s.previous_sample, str) or not re.fullmatch(r"[a-f0-9]{64}", s.previous_sample)):
        raise GuardError("invalid previous heartbeat")
    for value in (s.bad_since, s.last_observation):
        if value is not None:
            number(value)
    if s.guest_boot_id is None:
        if any(value is not None for value in (s.producer_instance, s.sample_monotonic, s.cycle_sequence)):
            raise GuardError("incomplete persisted heartbeat watermark")
        # Older states only retained a sample hash, which cannot prove order.
        s.recovery_count = 0
    else:
        if not isinstance(s.guest_boot_id, str) or not UUID.fullmatch(s.guest_boot_id):
            raise GuardError("invalid persisted guest boot identity")
        if s.producer_instance is not None and (not isinstance(s.producer_instance, str) or not re.fullmatch(r"[a-f0-9]{64}", s.producer_instance)):
            raise GuardError("invalid persisted producer identity")
        number(s.sample_monotonic)
        if type(s.cycle_sequence) is not int or s.cycle_sequence < 1:
            raise GuardError("invalid persisted cycle sequence")
    if s.management_boot_id != current_boot or raw.get("clock_domain") != CLOCK_DOMAIN:
        # A reboot or old/changed clock domain cannot transfer durations. In
        # particular, legacy macOS Python 3.9 times may be process-relative.
        # Preserve a qualified alarm; discard timing/recovery state and require
        # new observations. Never automatically clear a qualified alarm.
        return State(current_boot, alert=s.alert)
    return s


class Monitor:
    def __init__(self, config: Config, state: State):
        self.config, self.state = config, state

    def observe(self, value: dict | None, now: float, *, error=False) -> dict:
        number(now)
        s, c = self.state, self.config
        if s.last_observation is not None and now < s.last_observation:
            raise GuardError("management monotonic clock moved backwards")
        s.last_observation = now
        reason, age, sample = "guest-unreachable-or-invalid", None, None
        advancing, producer_changed = False, False
        if not error and value is not None:
            value = validate_heartbeat(value)
            age = value["sample_age_seconds"]
            sample = hashlib.sha256(json.dumps([value["guest_boot_id"], value.get("producer_instance"), value["sample_monotonic"],
                                               value["cycle_sequence"]], separators=(",", ":")).encode()).hexdigest()
            producer_changed = (s.guest_boot_id != value["guest_boot_id"] or
                                s.producer_instance != value.get("producer_instance"))
            advancing = (producer_changed or
                         value["sample_monotonic"] > s.sample_monotonic and
                         value["cycle_sequence"] > s.cycle_sequence)
            repeated = (not producer_changed and value["sample_monotonic"] == s.sample_monotonic and
                        value["cycle_sequence"] == s.cycle_sequence)
            reason = ("controller-stopped" if not value["controller_running"] else
                      "controller-dry-run" if value["dry_run"] else
                      "control-degraded" if value.get("control_degraded", False) else
                      "controller-reported-fault" if value["fault"] else
                      "floor-setting-unverified" if not value["floor_setting_verified"] else
                      "heartbeat-stale" if age > c.stale_after_seconds else
                      "heartbeat-not-advancing" if not advancing and not repeated else "healthy")
            if producer_changed:
                # A new producer must establish its own run of healthy cycles.
                s.recovery_count = 0
            if advancing:
                # Keep the high-water mark on regressions; otherwise a replayed
                # 2 -> 1 -> 2 sequence could manufacture three healthy cycles.
                s.guest_boot_id = value["guest_boot_id"]
                s.producer_instance = value.get("producer_instance")
                s.sample_monotonic = value["sample_monotonic"]
                s.cycle_sequence = value["cycle_sequence"]
        good = reason == "healthy"
        if not good:
            s.recovery_count = 0
            if s.bad_since is None:
                s.bad_since = now
            # Controller-declared faults/stops are explicit, not connectivity guesses.
            explicit = reason in ("controller-stopped", "controller-dry-run", "control-degraded", "controller-reported-fault", "floor-setting-unverified")
            if explicit or now - s.bad_since >= c.qualify_after_seconds:
                s.alert = True
        else:
            s.bad_since = None
            if s.alert:
                if advancing:
                    s.recovery_count += 1
                if s.recovery_count >= c.recovery_samples:
                    s.alert, s.recovery_count = False, 0
            else:
                s.recovery_count = 0
        if sample is not None:
            s.previous_sample = sample
        phase = "recovering" if good and s.alert else "alert" if s.alert else "healthy" if good else "qualifying"
        return {"schema": 1, "version": VERSION, "time_unix": time.time(), "mode": "monitor-only",
                "monitor_running": True, "phase": phase, "alert": s.alert, "reason": reason,
                "sample_age_seconds": age, "bad_for_seconds": 0 if s.bad_since is None else now - s.bad_since,
                "recovery_samples": s.recovery_count, "required_recovery_samples": c.recovery_samples,
                "bmc_write_attempted": False, "hardware_cooling_verified": False}


def write_private(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".external-guard-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def notify(message: str):
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(message.encode(), address)
    except OSError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    helper = commands.add_parser("heartbeat", help="guest-side read-only status helper")
    helper.add_argument("--status-file", type=Path, required=True)
    helper.add_argument("--status-format", choices=("generic", "production"), default="generic")
    watcher = commands.add_parser("monitor", help="independent-host read-only heartbeat monitor")
    watcher.add_argument("--config", type=Path, required=True)
    watcher.add_argument("--once", action="store_true", help="one observation; exit 0 healthy, 1 alert, 2 qualifying")
    args = parser.parse_args(argv)
    try:
        if args.command == "heartbeat":
            if not args.status_file.is_absolute():
                raise ConfigError("guest status path must be absolute")
            print(json.dumps(heartbeat(args.status_file, status_format=args.status_format), allow_nan=False))
            return 0
        c = load_config(args.config)
        watchdog = os.environ.get("WATCHDOG_USEC")
        if watchdog:
            try:
                budget = int(watchdog) / 1_000_000
            except ValueError as exc:
                raise ConfigError("invalid systemd watchdog interval") from exc
            if budget <= c.interval_seconds + c.command_timeout_seconds + 10:
                raise ConfigError("systemd watchdog budget too short")
        c.lock_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(c.lock_file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, "a") as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_uid not in (0, os.geteuid()):
                raise GuardError("external monitor lock must be a trusted regular file")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise GuardError("another external monitor owns this lock") from exc
            state = load_state(c.state_file, management_boot_id())
            monitor = Monitor(c, state)
            stop = threading.Event()
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stop.set())
            notify("READY=1")
            last_status = None
            last_logged_transition = None
            try:
                while True:
                    try:
                        value = fetch_heartbeat(c)
                    except GuardError:
                        value = None
                    last_status = monitor.observe(value, management_monotonic(), error=value is None)
                    write_private(c.state_file, dataclasses.asdict(state))
                    write_private(c.status_file, last_status)
                    transition = tuple(last_status[name] for name in ("phase", "reason", "alert", "monitor_running"))
                    if transition != last_logged_transition:
                        print(json.dumps(last_status, allow_nan=False), flush=True)
                        last_logged_transition = transition
                    notify("WATCHDOG=1\nSTATUS=" + last_status["phase"] + ": " + last_status["reason"])
                    if args.once or stop.wait(c.interval_seconds):
                        return 1 if last_status["alert"] else 0 if last_status["phase"] == "healthy" else 2
            finally:
                if last_status is not None:
                    final_status = {**last_status, "monitor_running": False, "time_unix": time.time()}
                    write_private(c.status_file, final_status)
                    print(json.dumps(final_status, allow_nan=False), flush=True)
                notify("STOPPING=1")
    except (GuardError, OSError) as exc:
        print(json.dumps({"schema": 1, "mode": "monitor-only", "alert": True, "monitor_running": False,
                          "reason": str(exc) if isinstance(exc, GuardError) else "local operating-system error",
                          "bmc_write_attempted": False}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
