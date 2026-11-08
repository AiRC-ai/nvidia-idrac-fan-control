#!/usr/bin/env python3
"""Render an existing fan status file as allowlisted JSON or Prometheus metrics.

This module executes no commands, opens no network connection, and never writes
fan settings. It supports generic schema 1 and the documented production shape.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
from pathlib import Path
import re
import stat
import time


class StatusError(ValueError):
    pass


def number(value, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    try:
        if not math.isfinite(value):
            return None
    except OverflowError:
        return None
    if low is not None and value < low or high is not None and value > high:
        return None
    return value


def boolean(value):
    return value if type(value) is bool else None


def timestamp(value):
    if isinstance(value, str) and len(value) <= 40:
        try:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.timestamp() if parsed.tzinfo is not None else None
        except (ValueError, OverflowError):
            return None
    return None


def read_status(path: Path) -> dict:
    # Refuse FIFOs/devices and symlinks; a scrape must not hang on a crafted path.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd) as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise StatusError("status-file-not-regular")
            text = handle.read(1024 * 1024 + 1)
        if len(text) > 1024 * 1024:
            raise StatusError("status-file-too-large")
        def unique_object(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise StatusError("ambiguous-status-object")
                value[key] = item
            return value
        value = json.loads(text, object_pairs_hook=unique_object)
    except (OSError, UnicodeError, ValueError, RecursionError, OverflowError) as exc:
        if isinstance(exc, StatusError):
            raise
        raise StatusError("status-unavailable-or-invalid") from exc
    if not isinstance(value, dict):
        raise StatusError("status-object-required")
    return value


def local_boot_id() -> str | None:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None
    return value if re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value) else None


def normalize_status(raw: dict, *, now: float, max_age: float, now_monotonic: float | None = None,
                     current_boot_id: str | None = None) -> dict:
    """Explicit adapters, finite values and fixed labels; arbitrary input never escapes."""
    if not isinstance(raw, dict) or number(now) is None or number(max_age, 1, 3600) is None:
        raise StatusError("invalid-status-or-clock")
    if raw.get("schema") == 1 and type(raw.get("schema")) is int and "time_unix" in raw:
        source = "generic-v1"
        observed = number(raw.get("time_unix"), 0)
        physical = raw.get("physical_diagnostics", {})
        floor_verified = boolean(raw.get("floor_setting_verified"))
        if floor_verified is None:
            floor_verified = boolean(raw.get("control_verified"))
        fault = boolean(raw.get("fault"))
        setting = {"age_seconds": raw.get("floor_setting_age_seconds")}
        gpu_id, memory_key, utilization_key, sample_clock = "index_in_sample", "memory_c", "utilization_pct", "sample_monotonic"
    elif "utc" in raw and "monotonic" in raw and "gpus" in raw and "requested_floor_pct" in raw:
        source = "production-v1"
        observed = timestamp(raw.get("utc"))
        diagnostics = raw.get("cooling_diagnostics", {})
        diagnostics = diagnostics if isinstance(diagnostics, dict) and diagnostics.get("schema") == 1 else {}
        physical = diagnostics.get("physical_diagnostics", {})
        setting = diagnostics.get("setting_proof", {})
        floor_verified = boolean(setting.get("verified")) if isinstance(setting, dict) else None
        if floor_verified is None:
            floor_verified = boolean(raw.get("control_verified"))
        fault = boolean(raw.get("fault_latched"))
        if setting and setting.get("fresh") is not True:
            floor_verified = False
        gpu_id, memory_key, utilization_key, sample_clock = "gpu", "hbm_c", "util_pct", "monotonic"
        # Older production chassis data contains measurements but no trustworthy
        # freshness/RPM calibration proof. Export values, not fabricated proofs.
        if not physical:
            chassis = raw.get("chassis_readback", {})
            if isinstance(chassis, dict):
                physical = {"fans": chassis.get("fans", []), "temperatures": chassis.get("temperatures", []),
                            "observed_time_unix": timestamp(chassis.get("utc"))}
    else:
        raise StatusError("unsupported-status-schema")
    if observed is None:
        raise StatusError("status-timestamp-unavailable")
    wall_age = now - observed
    sampled = number(raw.get(sample_clock), 0)
    boot = raw.get("boot_id")
    boot_matches = None
    age, freshness_clock = wall_age, "wall"
    if boot is not None and current_boot_id is not None:
        boot_matches = bool(isinstance(boot, str) and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot) and boot == current_boot_id)
        if boot_matches:
            if sampled is None or number(now_monotonic, 0) is None:
                raise StatusError("status-monotonic-unavailable")
            age, freshness_clock = now_monotonic - sampled, "monotonic"
    fresh = bool(boot_matches is not False and 0 <= age <= max_age)
    setting = setting if isinstance(setting, dict) else {}
    setting_age_at_status = number(setting.get("age_seconds"), 0)
    setting_age = age + setting_age_at_status if setting_age_at_status is not None else age
    setting_max_age = number(setting.get("max_age_seconds"), 1, 3600)
    effective_setting_max_age = min(max_age, setting_max_age) if setting_max_age is not None else max_age
    floor_verified = floor_verified if fresh and 0 <= setting_age <= effective_setting_max_age else False
    physical = physical if isinstance(physical, dict) else {}
    physical_age_at_status = number(physical.get("age_seconds"), 0)
    physical_observed = number(physical.get("observed_time_unix"), 0)
    physical_age = age + physical_age_at_status if physical_age_at_status is not None else now - physical_observed if physical_observed is not None else None
    source_max_age = number(physical.get("max_age_seconds"), 1, 3600)
    effective_physical_max_age = min(max_age, source_max_age) if source_max_age is not None else max_age
    # Cached true booleans do not stay true forever after the controller stops.
    physical_fresh = bool(fresh and physical.get("fresh") is True and physical_age is not None and 0 <= physical_age <= effective_physical_max_age)
    result = {"source": source, "status_available": True, "status_fresh": fresh,
              "status_age_seconds": max(0, age), "clock_in_future": age < 0,
              "freshness_clock": freshness_clock, "boot_matches": boot_matches,
              "controller_running": boolean(raw.get("controller_running")), "dry_run": boolean(raw.get("dry_run")),
              "fault": fault, "control_degraded": boolean(raw.get("control_degraded")),
              "floor_setting_verified": floor_verified, "floor_setting_age_seconds": max(0, setting_age),
              "requested_floor_pct": number(raw.get("requested_floor_pct"), 0, 100),
              "verified_floor_pct": number(raw.get("verified_floor_pct"), 0, 100),
              "physical_fresh": physical_fresh, "physical_age_seconds": max(0, physical_age) if physical_age is not None else None,
              "fan_pwm_verified": boolean(physical.get("fan_pwm_verified")) if physical_fresh else None,
              "fan_rpm_verified": boolean(physical.get("fan_rpm_verified")) if physical_fresh else None,
              "gpus": [], "fans": [], "temperatures": []}
    for row in raw.get("gpus", [])[:64] if isinstance(raw.get("gpus"), list) else []:
        if not isinstance(row, dict):
            continue
        index = row.get(gpu_id)
        if type(index) is not int or not 0 <= index <= 255 or any(r["index"] == index for r in result["gpus"]):
            continue
        result["gpus"].append({"index": index, "core_c": number(row.get("core_c"), 0, 130),
                               "memory_c": number(row.get(memory_key), 0, 140),
                               "power_w": number(row.get("power_w"), 0, 2000),
                               "utilization_pct": number(row.get(utilization_key), 0, 100)})
    for row in physical.get("fans", [])[:256] if isinstance(physical.get("fans"), list) else []:
        if not isinstance(row, dict):
            continue
        index = row.get("fan_id", row.get("fan"))
        if type(index) is not int or not 1 <= index <= 256 or any(r["fan_id"] == index for r in result["fans"]):
            continue
        result["fans"].append({"fan_id": index, "rpm": number(row.get("rpm"), 0, 100000),
                               "pwm_pct": number(row.get("pwm_pct", row.get("actual_pct")), 0, 100),
                               "healthy": str(row.get("health", "")).lower() == "ok" if "health" in row else None})
    for row in physical.get("temperatures", [])[:258] if isinstance(physical.get("temperatures"), list) else []:
        if not isinstance(row, dict) or row.get("kind") not in ("inlet", "exhaust", "cpu"):
            continue
        index = row.get("index")
        index = 0 if index is None and row["kind"] != "cpu" else index
        if type(index) is not int or not 0 <= index <= 256:
            continue
        result["temperatures"].append({"kind": row["kind"], "index": index, "temperature_c": number(row.get("temperature_c"), -40, 150)})
    return result


def prometheus(value: dict) -> str:
    lines = []
    def emit(name, sample, labels=""):
        if sample is not None:
            numeric = int(sample) if type(sample) is bool else number(sample)
            if numeric is not None:
                lines.append("nvidia_idrac_" + name + labels + " " + str(numeric))
    for name in ("status_available", "status_fresh", "status_age_seconds", "clock_in_future", "controller_running", "dry_run", "fault", "control_degraded",
                 "floor_setting_verified", "floor_setting_age_seconds", "requested_floor_pct", "verified_floor_pct", "physical_fresh", "physical_age_seconds"):
        emit(name, value.get(name))
    for name in ("fan_pwm_verified", "fan_rpm_verified"):
        emit(name + "_known", value.get(name) is not None)
        emit(name, value.get(name))
    for gpu in value.get("gpus", []):
        if not isinstance(gpu, dict) or type(gpu.get("index")) is not int or not 0 <= gpu["index"] <= 255:
            continue
        for key in ("core_c", "memory_c", "power_w", "utilization_pct"):
            emit("gpu_" + key, gpu.get(key), '{gpu="' + str(gpu["index"]) + '"}')
    for fan in value.get("fans", []):
        if not isinstance(fan, dict) or type(fan.get("fan_id")) is not int or not 1 <= fan["fan_id"] <= 256:
            continue
        for key in ("rpm", "pwm_pct", "healthy"):
            emit("fan_" + key, fan.get(key), '{fan="' + str(fan["fan_id"]) + '"}')
    for sensor in value.get("temperatures", []):
        if (not isinstance(sensor, dict) or sensor.get("kind") not in ("inlet", "exhaust", "cpu") or
                type(sensor.get("index")) is not int or not 0 <= sensor["index"] <= 256):
            continue
        emit("chassis_temperature_c", sensor.get("temperature_c"), '{kind="' + sensor["kind"] + '",sensor="' + str(sensor["index"]) + '"}')
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status-file", required=True, type=Path)
    parser.add_argument("--max-age-seconds", type=float, default=90)
    parser.add_argument("--format", choices=("prometheus", "json"), default="prometheus")
    args = parser.parse_args(argv)
    try:
        result = normalize_status(read_status(args.status_file), now=time.time(), max_age=args.max_age_seconds,
                                  now_monotonic=time.monotonic(), current_boot_id=local_boot_id())
    except StatusError:
        # Paths, arbitrary JSON strings and raw operating-system errors stay private.
        result = {"status_available": False, "status_fresh": False}
        print(json.dumps(result) if args.format == "json" else prometheus(result), end="\n" if args.format == "json" else "")
        return 2
    print(json.dumps(result, allow_nan=False) if args.format == "json" else prometheus(result), end="\n" if args.format == "json" else "")
    return 0 if result["status_fresh"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
