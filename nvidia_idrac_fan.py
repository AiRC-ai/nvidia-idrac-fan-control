#!/usr/bin/env python3
"""Raise an iDRAC automatic fan floor from local NVIDIA telemetry. Stdlib only."""
from __future__ import annotations

import argparse
import dataclasses
import fcntl
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET

VERSION = "0.1.0"
THERMAL_OBJECT = "System.ThermalSettings"
MINIMUM_OBJECT = THERMAL_OBJECT + ".MinimumFanSpeed"


class FanError(Exception):
    """A bounded diagnostic code; subprocess output is deliberately not logged."""


class ConfigError(FanError):
    pass


class TelemetryError(FanError):
    pass


class ControlError(FanError):
    pass


@dataclasses.dataclass(frozen=True)
class GPU:
    identity: str
    core_c: float
    memory_c: float | None
    power_w: float
    utilization_pct: float


@dataclasses.dataclass(frozen=True)
class ThermalSettings:
    minimum_pct: int
    lower_limit: int
    upper_limit: int
    pci_response: int


@dataclasses.dataclass(frozen=True)
class PolicyConfig:
    idle_floor_pct: int = 55
    active_floor_pct: int = 84
    active_power_w: float = 80
    active_utilization_pct: float = 15
    core_target_c: float = 48
    memory_target_c: float = 55
    gain_pct_per_c: float = 2
    core_full_speed_c: float = 75
    memory_full_speed_c: float = 80
    core_critical_c: float = 80
    memory_critical_c: float = 85
    down_step_pct: int = 5
    cooldown_seconds: float = 60
    down_hysteresis_pct: int = 3

    def validate(self):
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ConfigError("policy values must be finite numbers")
        for name in ("idle_floor_pct", "active_floor_pct", "down_step_pct", "down_hysteresis_pct"):
            if type(getattr(self, name)) is not int:
                raise ConfigError("fan percentages and steps must be integers")
        if not 1 <= self.idle_floor_pct <= self.active_floor_pct <= 100:
            raise ConfigError("fan floors must satisfy 1 <= idle <= active <= 100")
        if not 0 <= self.active_utilization_pct <= 100 or not 0 < self.active_power_w <= 2000:
            raise ConfigError("invalid activity thresholds")
        if not 0 < self.core_target_c < self.core_full_speed_c < self.core_critical_c <= 120:
            raise ConfigError("invalid core temperature thresholds")
        if not 0 < self.memory_target_c < self.memory_full_speed_c < self.memory_critical_c <= 130:
            raise ConfigError("invalid memory temperature thresholds")
        if not 0 < self.gain_pct_per_c <= 20 or not 1 <= self.down_step_pct <= 20:
            raise ConfigError("invalid gain or reduction step")
        if not 1 <= self.cooldown_seconds <= 3600 or not 1 <= self.down_hysteresis_pct <= 20:
            raise ConfigError("invalid cooldown or hysteresis")


@dataclasses.dataclass(frozen=True)
class Config:
    host: str
    user: str
    identity_file: Path
    known_hosts_file: Path
    expected_gpu_count: int
    require_memory_temperature: bool
    status_file: Path
    lock_file: Path
    policy: PolicyConfig
    ssh_port: int = 22
    interval_seconds: float = 5
    connect_timeout_seconds: int = 5
    command_timeout_seconds: int = 15
    nvidia_smi: str = "/usr/bin/nvidia-smi"
    ssh: str = "/usr/bin/ssh"


def load_config(path: Path) -> Config:
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ConfigError("configuration must be a JSON object")
        raw = dict(raw)
        raw["policy"] = PolicyConfig(**raw.pop("policy", {}))
        for name in ("identity_file", "known_hosts_file", "status_file", "lock_file"):
            raw[name] = Path(raw[name]).expanduser()
            if not raw[name].is_absolute():
                raise ConfigError("all configured file paths must be absolute")
        c = Config(**raw)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ConfigError("cannot parse configuration; check required keys and types") from exc
    c.policy.validate()
    if not isinstance(c.host, str) or not isinstance(c.user, str):
        raise ConfigError("SSH host and user must be strings")
    try:
        ipaddress.ip_address(c.host)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", c.host):
            raise ConfigError("invalid SSH hostname")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", c.user):
        raise ConfigError("invalid SSH username")
    for key, low, high in (("ssh_port", 1, 65535), ("expected_gpu_count", 1, 64),
                           ("connect_timeout_seconds", 1, 30), ("command_timeout_seconds", 2, 60)):
        value = getattr(c, key)
        if type(value) is not int or not low <= value <= high:
            raise ConfigError("invalid " + key)
    if type(c.require_memory_temperature) is not bool:
        raise ConfigError("require_memory_temperature must be boolean")
    if isinstance(c.interval_seconds, bool) or not isinstance(c.interval_seconds, (int, float)) or not math.isfinite(c.interval_seconds) or not 1 <= c.interval_seconds <= 60:
        raise ConfigError("interval_seconds must be between 1 and 60")
    for command in (c.ssh, c.nvidia_smi):
        if not isinstance(command, str) or not Path(command).is_absolute():
            raise ConfigError("executable paths must be absolute")
    # OpenSSH expands tokens and parses filename lists for these options. Refuse
    # ambiguous names rather than relying on shell-style escaping (there is no shell).
    for ssh_path in (c.identity_file, c.known_hosts_file):
        if re.search(r"[\s%$\"'\\]", str(ssh_path)):
            raise ConfigError("SSH file paths cannot contain whitespace, percent, quotes, dollar signs or backslashes")
    paths = [path.expanduser(), c.identity_file, c.known_hosts_file, c.status_file, c.lock_file]
    try:
        for index, first in enumerate(paths):
            for second in paths[index + 1:]:
                if first.resolve() == second.resolve() or first.exists() and second.exists() and first.samefile(second):
                    raise ConfigError("configuration, SSH, status and lock files must be distinct")
    except (OSError, RuntimeError) as exc:
        raise ConfigError("cannot safely resolve configured file paths") from exc
    try:
        key_stat = c.identity_file.stat()
        if not stat.S_ISREG(key_stat.st_mode) or key_stat.st_mode & 0o077:
            raise ConfigError("SSH identity must be a regular file with mode 0600 or stricter")
        trust_stat = c.known_hosts_file.stat()
        if not stat.S_ISREG(trust_stat.st_mode) or not trust_stat.st_size or trust_stat.st_mode & 0o022:
            raise ConfigError("a populated, independently verified known_hosts file is required")
    except OSError as exc:
        raise ConfigError("SSH identity or known_hosts file is unavailable") from exc
    return c


def _number(text: str | None, unit: str, low: float, high: float, *, optional=False):
    value = (text or "").strip()
    if optional and value in ("", "N/A", "Not Supported", "[Not Supported]"):
        return None
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*" + re.escape(unit), value)
    if not match:
        raise TelemetryError("missing or malformed NVIDIA reading")
    result = float(match[1])
    if not math.isfinite(result) or not low <= result <= high:
        raise TelemetryError("NVIDIA reading outside allowed range")
    return result


def parse_nvidia_xml(text: str, expected_count: int, require_memory: bool) -> list[GPU]:
    if len(text) > 4 * 1024 * 1024 or "<!ENTITY" in text.upper():
        raise TelemetryError("unexpected NVIDIA XML size or entity declaration")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise TelemetryError("invalid NVIDIA XML") from exc
    nodes = root.findall("gpu")
    if root.tag != "nvidia_smi_log" or len(nodes) != expected_count:
        raise TelemetryError("GPU inventory count mismatch")
    result = []
    for gpu in nodes:
        identity = (gpu.findtext("uuid") or "").strip()
        if not identity.startswith("GPU-") or any(g.identity == identity for g in result):
            raise TelemetryError("missing or duplicate GPU identity")
        memory_values = [gpu.findtext("temperature/" + name) for name in ("memory_temp", "gpu_memory_temp")]
        memory_values = [v for v in memory_values if v is not None]
        if len(memory_values) > 1 and len(set(memory_values)) != 1:
            raise TelemetryError("ambiguous memory temperature")
        memory = _number(memory_values[0] if memory_values else None, "C", 0, 140, optional=not require_memory)
        power_values = []
        for group in ("gpu_power_readings", "power_readings"):
            for field in ("instant_power_draw", "power_draw", "average_power_draw"):
                value = gpu.findtext(group + "/" + field)
                if value is not None and value.strip() not in ("N/A", "Not Supported", "[Not Supported]"):
                    power_values.append(_number(value, "W", 0, 2000))
        if not power_values:
            raise TelemetryError("NVIDIA power telemetry unavailable")
        result.append(GPU(identity, _number(gpu.findtext("temperature/gpu_temp"), "C", 0, 130),
                          memory, max(power_values), _number(gpu.findtext("utilization/gpu_util"), "%", 0, 100)))
    return result


def parse_thermal_settings(text: str) -> ThermalSettings:
    if len(text) > 128 * 1024:
        raise ControlError("unexpected RACADM response size")
    required = {"minimumfanspeed", "mfsminimumlimit", "mfsmaximumlimit", "thirdpartypcifanresponse"}
    values = {}
    for line in text.splitlines():
        match = re.fullmatch(r"\s*#?\s*([A-Za-z][A-Za-z0-9 ]*)\s*=\s*(.*?)\s*", line)
        if not match:
            continue
        key = re.sub(r"[^a-z0-9]", "", match[1].lower())
        if key not in required:
            continue
        if key in values:
            raise ControlError("ambiguous RACADM thermal settings")
        value = match[2]
        if key == "thirdpartypcifanresponse" and value in ("Enabled", "Disabled"):
            value = "1" if value == "Enabled" else "0"
        if not re.fullmatch(r"\d{1,3}", value):
            raise ControlError("unsupported RACADM thermal setting format")
        values[key] = int(value)
    if set(values) != required:
        raise ControlError("required minimum-fan/PCI-response capabilities absent; this iDRAC is unsupported")
    t = ThermalSettings(values["minimumfanspeed"], values["mfsminimumlimit"],
                        values["mfsmaximumlimit"], values["thirdpartypcifanresponse"])
    if not 0 <= t.lower_limit <= t.upper_limit == 100:
        raise ControlError("advertised fan range does not permit a verified 100-percent fail-safe")
    if t.minimum_pct != 255 and not t.lower_limit <= t.minimum_pct <= t.upper_limit:
        raise ControlError("invalid configured fan floor")
    if t.pci_response != 1:
        raise ControlError("third-party PCI fan response must already be enabled; this tool will not change it")
    return t


def run_command(argv: list[str], timeout: float, error_type=ControlError) -> str:
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise error_type("command unavailable or timed out") from exc
    if result.returncode != 0:
        raise error_type("command failed; inspect connectivity independently")
    if len(result.stdout) > 4 * 1024 * 1024:
        raise error_type("command response too large")
    return result.stdout


class IDRAC:
    def __init__(self, c: Config, runner=run_command):
        self.config = c
        self.runner = runner

    def command(self, args: list[str]) -> str:
        c = self.config
        # Remote arguments are fixed RACADM words and validated integers only.
        command = [c.ssh, "-F", "/dev/null", "-T", "-p", str(c.ssh_port), "-i", str(c.identity_file)]
        for option in ("BatchMode=yes", "PasswordAuthentication=no", "KbdInteractiveAuthentication=no",
                       "IdentitiesOnly=yes", "StrictHostKeyChecking=yes", "GlobalKnownHostsFile=/dev/null",
                       "UserKnownHostsFile=" + str(c.known_hosts_file),
                       "ConnectTimeout=" + str(c.connect_timeout_seconds), "ConnectionAttempts=1",
                       "ServerAliveInterval=5", "ServerAliveCountMax=1", "LogLevel=ERROR"):
            command.extend(["-o", option])
        command.extend([c.user + "@" + c.host, "racadm", *args])
        return self.runner(command, c.command_timeout_seconds)

    def probe(self) -> ThermalSettings:
        settings = parse_thermal_settings(self.command(["get", THERMAL_OBJECT]))
        if self.config.policy.idle_floor_pct < settings.lower_limit:
            raise ControlError("configured idle floor is below the advertised iDRAC minimum")
        return settings

    def set_floor(self, value: int) -> ThermalSettings:
        if type(value) is not int or not 1 <= value <= 100:
            raise ControlError("invalid fan-floor request")
        reply = self.command(["set", MINIMUM_OBJECT, str(value)])
        if not re.search(r"(?m)^\s*Object value modified successfully\.?\s*$", reply):
            raise ControlError("RACADM did not acknowledge the requested setting")
        current = self.probe()
        if current.minimum_pct != value:
            raise ControlError("RACADM readback does not match the requested floor")
        return current


class Policy:
    def __init__(self, config: PolicyConfig):
        config.validate()
        self.config = config
        self.current = 100
        self.lower_since: float | None = None

    def fault(self):
        self.current = 100
        self.lower_since = None

    def choose(self, gpus: list[GPU], now: float) -> tuple[int, str]:
        if not gpus:
            raise TelemetryError("empty GPU inventory")
        c = self.config
        core = max(g.core_c for g in gpus)
        memory = max((g.memory_c for g in gpus if g.memory_c is not None), default=None)
        active = any(g.power_w >= c.active_power_w or g.utilization_pct >= c.active_utilization_pct for g in gpus)
        if core >= c.core_critical_c or memory is not None and memory >= c.memory_critical_c:
            self.fault()
            return 100, "critical-temperature-no-workload-stop"
        if core >= c.core_full_speed_c or memory is not None and memory >= c.memory_full_speed_c:
            self.fault()
            return 100, "high-temperature"
        demand = max(c.idle_floor_pct, c.idle_floor_pct + (core - c.core_target_c) * c.gain_pct_per_c)
        if memory is not None:
            demand = max(demand, c.idle_floor_pct + (memory - c.memory_target_c) * c.gain_pct_per_c)
        demand = min(100, math.ceil(max(demand, c.active_floor_pct if active else c.idle_floor_pct)))
        if demand >= self.current:
            self.current, self.lower_since = demand, None
        elif demand <= self.current - c.down_hysteresis_pct:
            if self.lower_since is None:
                self.lower_since = now
            if now - self.lower_since >= c.cooldown_seconds:
                self.current = max(demand, self.current - c.down_step_pct)
                self.lower_since = now
        else:
            self.lower_since = None
        return self.current, "active" if active else "idle-thermal-demand"


class Controller:
    def __init__(self, c: Config, idrac: IDRAC, *, apply: bool, telemetry=None):
        self.config, self.idrac, self.apply = c, idrac, apply
        self.policy = Policy(c.policy)
        self.telemetry = telemetry or self.read_gpus
        self.identities: set[str] | None = None
        self.memory_available: set[str] = set()
        self.control_verified = False
        self.last_floor: int | None = None

    def read_gpus(self):
        text = run_command([self.config.nvidia_smi, "-q", "-x"], self.config.command_timeout_seconds, TelemetryError)
        return parse_nvidia_xml(text, self.config.expected_gpu_count, self.config.require_memory_temperature)

    def failsafe(self) -> bool:
        self.policy.fault()
        self.control_verified = False
        if not self.apply:
            return False
        try:
            self.idrac.set_floor(100)
            self.last_floor, self.control_verified = 100, True
        except FanError:
            return False
        return True

    def step(self, now: float) -> dict:
        samples = []
        try:
            samples = self.telemetry()
            identities = {g.identity for g in samples}
            if len(samples) != self.config.expected_gpu_count or len(identities) != len(samples):
                raise TelemetryError("GPU inventory mismatch")
            if self.identities is not None and identities != self.identities:
                raise TelemetryError("GPU identities changed")
            self.identities = identities
            memory = {g.identity for g in samples if g.memory_c is not None}
            if not self.memory_available <= memory:
                raise TelemetryError("previously available memory telemetry disappeared")
            self.memory_available |= memory
            settings = self.idrac.probe()  # Check vendor PCI protection every cycle, including dry runs.
            desired, reason = self.policy.choose(samples, now)
            if self.apply:
                if settings.minimum_pct != desired or not self.control_verified:
                    self.idrac.set_floor(desired)
                self.last_floor, self.control_verified = desired, True
            critical = reason == "critical-temperature-no-workload-stop"
            return self.status(samples, desired, reason, critical)
        except FanError as exc:
            self.failsafe()  # Every failed cycle retries 100; no inference stop or process kill.
            return self.status(samples, 100, type(exc).__name__ + ": " + str(exc), True)

    def status(self, samples, desired, reason, fault, *, running=True):
        return {"schema": 1, "time_unix": time.time(), "version": VERSION, "dry_run": not self.apply,
                "controller_running": running,
                "requested_floor_pct": desired, "verified_floor_pct": self.last_floor if self.control_verified else None,
                "control_verified": self.control_verified, "fault": fault, "reason": reason,
                "gpus": [{"index_in_sample": i, "core_c": g.core_c, "memory_c": g.memory_c,
                          "power_w": g.power_w, "utilization_pct": g.utilization_pct} for i, g in enumerate(samples)]}


def write_status(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".fan-status-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


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
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="permit verified MinimumFanSpeed writes; default is dry-run")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--probe", action="store_true", help="read capabilities and telemetry once, without writes")
    mode.add_argument("--once", action="store_true", help="perform one policy cycle")
    mode.add_argument("--failsafe", action="store_true", help="request/read back 100 once; requires --apply")
    args = parser.parse_args(argv)
    try:
        c = load_config(args.config)
        if args.probe and args.apply or args.failsafe and not args.apply:
            raise ConfigError("--probe cannot write; --failsafe requires --apply")
        c.lock_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with c.lock_file.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ConfigError("another controller holds the configured lock") from exc
            idrac = IDRAC(c)
            settings = idrac.probe()  # Unsupported firmware: refuse writes, explain, leave policy untouched.
            controller = Controller(c, idrac, apply=args.apply)
            if args.probe:
                samples = controller.read_gpus()
                print(json.dumps({"schema": 1, "dry_run": True, "thermal_settings": dataclasses.asdict(settings),
                                  "gpu_count": len(samples), "memory_temperature_available": [g.memory_c is not None for g in samples]}))
                return 0
            if args.failsafe:
                ok = controller.failsafe()
                value = controller.status([], 100, "operator-failsafe", not ok, running=False)
                write_status(c.status_file, value)
                print(json.dumps(value))
                return 0 if ok else 1
            watchdog_usec = os.environ.get("WATCHDOG_USEC")
            if watchdog_usec:
                try:
                    watchdog_seconds = int(watchdog_usec) / 1_000_000
                except ValueError as exc:
                    raise ConfigError("invalid systemd watchdog interval") from exc
                if watchdog_seconds <= c.interval_seconds + 6 * c.command_timeout_seconds + 10:
                    raise ConfigError("systemd watchdog is too short for configured command timeouts")
            stop = threading.Event()
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stop.set())
            if args.apply and not controller.failsafe():
                raise ControlError("startup 100-percent request could not be verified")
            notify("READY=1")
            exit_code = 0
            try:
                while True:
                    value = controller.step(time.monotonic())
                    write_status(c.status_file, value)
                    print(json.dumps(value, allow_nan=False), flush=True)
                    notify("WATCHDOG=1\nSTATUS=" + ("fault; maximum requested" if value["fault"] else value["reason"]))
                    if args.once or stop.wait(c.interval_seconds):
                        exit_code = 1 if value["fault"] else 0
                        break
            finally:
                if args.apply:
                    # Leave a persistent high floor on graceful exit. Never restore a low floor automatically.
                    ok = controller.failsafe()
                    write_status(c.status_file, controller.status([], 100, "controller-stopped", not ok, running=False))
                    notify("STOPPING=1")
                    if not ok:
                        exit_code = 1
            return exit_code
    except (FanError, OSError) as exc:
        print(json.dumps({"fault": True, "error": type(exc).__name__, "detail": str(exc) if isinstance(exc, FanError) else "local file or operating-system error"}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
