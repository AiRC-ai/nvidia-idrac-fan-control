"""Optional RACADM sensor diagnostics. No actuation or subprocess execution."""
from __future__ import annotations

import dataclasses
import math
import re


class DiagnosticError(ValueError):
    """A fixed, non-sensitive diagnostic code."""


@dataclasses.dataclass(frozen=True)
class DiagnosticsConfig:
    enabled: bool = True
    interval_seconds: float = 30
    max_age_seconds: float = 90
    expected_fan_ids: list[int] | None = None
    rpm_calibration_approved: bool = False
    rpm_minimums: dict[str, list[list[int]]] = dataclasses.field(default_factory=dict)

    def validate(self):
        if type(self.enabled) is not bool or type(self.rpm_calibration_approved) is not bool:
            raise DiagnosticError("diagnostic flags must be boolean")
        for value in (self.interval_seconds, self.max_age_seconds):
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 1 <= value <= 3600:
                raise DiagnosticError("diagnostic intervals must be finite, between 1 and 3600 seconds")
        if self.max_age_seconds < self.interval_seconds:
            raise DiagnosticError("diagnostic maximum age cannot be below its polling interval")
        ids = self.expected_fan_ids
        if ids is not None and (not isinstance(ids, list) or not ids or len(ids) > 256 or
                                any(type(i) is not int or not 1 <= i <= 256 for i in ids) or len(set(ids)) != len(ids)):
            raise DiagnosticError("expected fan IDs must be distinct integers between 1 and 256")
        if not isinstance(self.rpm_minimums, dict) or len(self.rpm_minimums) > 256:
            raise DiagnosticError("invalid RPM calibration")
        if self.rpm_minimums and (not self.rpm_calibration_approved or ids is None):
            raise DiagnosticError("RPM calibration requires explicit approval and configured fan IDs")
        if self.rpm_minimums and set(self.rpm_minimums) != {str(i) for i in ids}:
            raise DiagnosticError("RPM calibration must cover exactly the configured fans")
        for key, points in self.rpm_minimums.items():
            if not isinstance(key, str) or not re.fullmatch(r"[1-9][0-9]{0,2}", key) or not isinstance(points, list) or not 1 <= len(points) <= 100:
                raise DiagnosticError("invalid RPM calibration")
            previous_pwm, previous_rpm = 0, 0
            for point in points:
                if (not isinstance(point, list) or len(point) != 2 or any(type(v) is not int for v in point) or
                        not previous_pwm < point[0] <= 100 or not previous_rpm <= point[1] <= 100000 or point[1] < 1):
                    raise DiagnosticError("RPM calibration points must increase by PWM and not decrease by RPM")
                previous_pwm, previous_rpm = point


def _health(value: str) -> str:
    value = value.lower()
    if value == "ok":
        return "ok"
    if value in ("warning", "critical", "failed", "absent", "unknown"):
        return value
    return "unknown"


def parse_chassis_sensors(text: str) -> dict:
    """Conservatively parse familiar getsensorinfo rows; unsupported fields stay unknown.

    Accept no arbitrary sensor names into output. RPM is the first reading after
    health, not a threshold column. PWM is accepted only as a final percentage.
    Partial/malformed/duplicate fan rows cannot establish whole-inventory proof.
    """
    if not isinstance(text, str) or len(text) > 512 * 1024:
        raise DiagnosticError("diagnostic-response-size")
    fans, temperatures, issues = {}, {}, set()
    fan_prefix = re.compile(r"^\s*(?:(?:System Board|System)\s+)?Fan\s*([0-9]{1,3})\b(.*)$", re.I)
    temp_prefix = re.compile(r"^\s*(?:(?:System Board|System)\s+)?(Inlet Temp|Exhaust Temp|CPU\s*([0-9]{1,3})\s+Temp)\s+(.*)$", re.I)
    for line in text.splitlines():
        match = fan_prefix.match(line)
        if match:
            identity, rest = int(match[1]), match[2].strip()
            if not 1 <= identity <= 256:
                issues.add("unsupported-fan-id")
                continue
            if identity in fans:
                issues.add("duplicate-fan-row")
                fans[identity] = {"fan_id": identity, "health": "unknown", "rpm": None, "pwm_pct": None}
                continue
            fields = rest.split()
            health = _health(fields[0]) if fields else "unknown"
            reading = re.match(r"^\S+\s+([0-9]{1,6})\s*RPM\b", rest, re.I)
            pwm = re.search(r"(?:^|\s)([0-9]{1,3})\s*%\s*$", rest)
            rpm = int(reading[1]) if reading and int(reading[1]) <= 100000 else None
            pct = int(pwm[1]) if pwm and int(pwm[1]) <= 100 else None
            if rpm is None or pct is None or health == "unknown":
                issues.add("incomplete-fan-row")
            fans[identity] = {"fan_id": identity, "health": health, "rpm": rpm, "pwm_pct": pct}
            continue
        if re.match(r"^\s*(?:System Board\s+)?Fan\b", line, re.I):
            issues.add("unsupported-fan-row")
        match = temp_prefix.match(line)
        if match:
            kind = "cpu" if match[2] else "inlet" if match[1].lower().startswith("inlet") else "exhaust"
            index = int(match[2]) if match[2] else 0
            if index > 256:
                issues.add("unsupported-temperature-row")
                continue
            rest = match[3].strip()
            fields = rest.split()
            reading = re.match(r"^\S+\s+(-?[0-9]+(?:\.[0-9]+)?)\s*(?:degrees\s+C|C)\b", rest, re.I)
            value = float(reading[1]) if reading else None
            if value is not None and not -40 <= value <= 150:
                value = None
            key = kind, index
            if key in temperatures:
                value = None
                issues.add("duplicate-temperature-row")
            temperatures[key] = {"kind": kind, "index": index, "health": _health(fields[0]) if fields else "unknown", "temperature_c": value}
    if not fans:
        issues.add("fan-telemetry-unavailable")
    return {"fans": [fans[i] for i in sorted(fans)], "temperatures": [temperatures[i] for i in sorted(temperatures)], "issues": sorted(issues)}


def assess_snapshot(snapshot: dict | None, config: DiagnosticsConfig, *, age_seconds: float | None,
                    requested_floor: int | None, last_read_ok: bool) -> dict:
    """Return tri-state evidence; positive RPM alone is never a sufficiency proof."""
    fresh = bool(config.enabled and snapshot is not None and last_read_ok and age_seconds is not None and 0 <= age_seconds <= config.max_age_seconds)
    fans = [dict(fan, rpm_nonzero=fan["rpm"] > 0 if fan["rpm"] is not None else None,
                 rpm_minimum_required=None, rpm_meets_minimum=None) for fan in (snapshot or {}).get("fans", [])]
    expected = set(config.expected_fan_ids or [])
    observed = {f["fan_id"] for f in fans}
    complete = expected == observed if expected else None
    usable = fresh and bool(fans) and not (snapshot or {}).get("issues")
    pwm = None
    rpm_proof = None
    if complete is False:
        pwm = False if fresh else None
        rpm_proof = False if fresh and config.rpm_calibration_approved and config.rpm_minimums else None
    elif complete and usable:
        healthy = all(f["health"] == "ok" for f in fans)
        if requested_floor is not None:
            pwm = healthy and all(f["pwm_pct"] is not None and f["pwm_pct"] >= requested_floor for f in fans)
        if config.rpm_calibration_approved and config.rpm_minimums:
            checks = []
            for fan in fans:
                points = config.rpm_minimums[str(fan["fan_id"])]
                thresholds = [minimum for duty, minimum in points if fan["pwm_pct"] is not None and duty <= fan["pwm_pct"]]
                fan["rpm_minimum_required"] = max(thresholds) if thresholds else None
                fan["rpm_meets_minimum"] = fan["rpm"] is not None and fan["rpm"] >= max(thresholds) if thresholds else None
                checks.append(fan["rpm_meets_minimum"])
            if all(check is not None for check in checks):
                rpm_proof = healthy and all(checks)
    capabilities = {"fan_rpm": any(f["rpm"] is not None for f in fans),
                    "fan_pwm": any(f["pwm_pct"] is not None for f in fans),
                    "chassis_temperatures": any(t["temperature_c"] is not None for t in (snapshot or {}).get("temperatures", []))}
    state = ("disabled" if not config.enabled else "unavailable" if snapshot is None or not last_read_ok else
             "stale" if not fresh else "unsupported" if not fans else "incomplete" if not usable or complete is False else "ok")
    return {"enabled": config.enabled, "available": bool(fans), "fresh": fresh, "state": state,
            "capabilities": capabilities,
            "max_age_seconds": config.max_age_seconds,
            "age_seconds": age_seconds, "inventory_complete_verified": bool(complete and usable),
            "inventory_scope": "configured" if expected else "discovered-only",
            "expected_fan_ids": sorted(expected) if expected else None,
            "missing_fan_ids": sorted(expected - observed), "unexpected_fan_ids": sorted(observed - expected) if expected else [],
            "fan_pwm_verified": pwm, "fan_rpm_verified": rpm_proof,
            "rpm_calibration_approved": config.rpm_calibration_approved,
            "fans": fans, "temperatures": (snapshot or {}).get("temperatures", []),
            "issues": (snapshot or {}).get("issues", [])}
