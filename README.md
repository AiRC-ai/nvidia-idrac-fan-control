# NVIDIA → iDRAC fan-floor controller

A small Linux Python 3.10+ controller that reads local NVIDIA GPU core and supported memory temperatures, power, and utilization. It raises the server's **automatic minimum fan speed** over authenticated SSH/RACADM. It uses the Python standard library, OpenSSH, and the installed NVIDIA driver tools.

The policy is dynamic: idle GPUs can cool down even while a model remains loaded. Rising temperatures, including supported HBM readings, increase the requested fan floor. Firmware can always run the fans faster.

**Experimental hardware-control software. Use at your own risk.** Inadequate airflow, failed fans, an unreachable BMC, or a stopped controller can still cause overheating. This is not a certified thermal safety system. It does not stop workloads, change GPU power limits, or override hardware thermal protection.

## Compatibility and validation status

The underlying policy concepts were developed in a real **Dell PowerEdge R730 / iDRAC8**, with **two NVIDIA Tesla V100 PCIe 32 GB cards**, exposed to a **Debian GPU guest on ESXi 7**. This reusable package is a separate clean implementation. Its offline controller, diagnostics, and cached-exporter tests pass; see [VALIDATION.md](VALIDATION.md) for the measured suite and acceptance boundaries. The **version 0.1.0 read-only capability probe passed on that R730**: both V100s and both memory-temperature sensors were detected, with an advertised 0–100% fan-floor range and PCI fan response enabled. That probe made no hardware-setting writes and does not validate the later 0.2.0 diagnostics. **Continuous control, workload cooling, and systemd/reboot behavior of this generic package remain unvalidated.** Experience with the original installation is not acceptance of this artifact.

Other iDRAC generations, firmware revisions, GPU models, Linux distributions, and bare-metal deployments are untested. They may require different commands or may not support these controls. Do not assume compatibility because SSH works. The controller refuses writes when its required capability checks fail.

It requires all of the following on the target iDRAC:

- `System.ThermalSettings.MinimumFanSpeed`, `MFSMinimumLimit`, and `MFSMaximumLimit` in the expected RACADM format.
- An advertised fan-floor range that includes **100%**. A lower firmware maximum is rejected.
- `ThirdPartyPCIFanResponse` already enabled (`1`). This program never disables or changes that protection.
- An acknowledgement of each write and an independent matching readback of `MinimumFanSpeed`.

The controller changes only `MinimumFanSpeed`; it does not use raw IPMI/manual fan mode, alter `FanSpeedOffset`, thermal profiles, PCI airflow requirements, BIOS, BMC firmware, accounts, or SSH policy. It cannot determine whether another administrator already disabled automatic fan mode through a different interface: verify vendor automatic control independently before deployment.

## How the policy works

All configured GPUs participate. Device count and identities must remain stable. Core temperature, board power, and utilization are mandatory. Memory temperature is mandatory in the example V100 configuration; other devices can explicitly permit unsupported memory readings. A memory sensor that was available and then disappears is always a fault.

The example values are conservative starting points, **not universal GPU limits**:

| Condition | Example response |
|---|---|
| Cool idle | Floor of 55%, after delayed stepwise reductions |
| GPU utilization ≥15% or board power ≥80 W | At least 84% |
| Core above 48°C or memory above 55°C | Add 2 percentage points per degree over target, keeping the larger demand |
| Core ≥75°C or memory ≥80°C | Request 100% |
| Core ≥80°C or memory ≥85°C | Request 100%, report critical; workload continues |
| Telemetry or control failure | Request/read back 100%; retry every failed cycle |
| Graceful controller exit | Request/read back 100%; leave it persistent |

Increases occur on the next successful observation and control cycle. Reductions require a 60-second stable lower demand, then drop at most 5 percentage points per 60 seconds. Startup begins at 100%. A fault resets the reduction timer. An idle reduction from 100% to 55% consequently takes at least nine minutes with these settings.

Polling and SSH are not real-time. Each external command has a timeout, and the configured polling delay is added after a cycle. Under failures, a cycle can take several command timeouts. The service watchdog must exceed the worst-case bound; the program checks its configured watchdog budget.

### Important limits

`control_verified=true` (also `floor_setting_verified=true`) proves the **floor setting** was read back. It does not prove fan RPM, heatsink airflow, sensor calibration, thermal headroom, or safe sustained GPU operation. The separate physical diagnostic fields below describe narrower, optional evidence. Dell's automatic algorithm may keep fans above the requested floor; this program cannot force them lower.

100% is a requested setting, not a guarantee when SSH or the BMC fails. Hard power loss, kernel failure, `SIGKILL`, bad networking, or unavailable management access can prevent the final request. The last persistent floor can remain set. Keep Dell's automatic cooling and hardware protections enabled, and use independent monitoring. Never run another fan controller against the same BMC at the same time.

The program intentionally does **not** stop inference, kill processes, reset GPUs, clear cooling faults, change power caps, or replay client requests. An operator must intervene if 100% fans cannot control temperatures.

## Review and configure

1. Confirm the chassis has the correct fans, shrouds, heatsinks, power connections, slot blanks, and GPU airflow. Consult the server and GPU documentation.
2. Use a dedicated local Linux service account and a dedicated iDRAC account/key with the minimum permissions that allow reading and updating the thermal setting. iDRAC permission granularity varies; the program does not claim to enforce a BMC-side command restriction.
3. Verify the iDRAC SSH host-key fingerprint using an independently trusted path such as its console or existing administrator records. Populate a dedicated `known_hosts` file with the verified key. **Do not trust an unverified `ssh-keyscan` result.**
4. Copy `config.example.json` to a private local configuration and substitute your own hostname, username, key/trust files, device count, and conservative temperature policy. No passwords or private keys belong in JSON or command-line arguments.

The SSH transport disables interactive/password authentication, requires a specific identity and known-hosts file, and enables strict host-key checks. The private key must have mode 0600 or stricter; the trust file must not be group/world writable. SSH file paths must avoid whitespace and OpenSSH filename-expansion metacharacters. Configuration, key, trust, status, and lock paths must all be distinct, including symlink/hardlink aliases. Use an owner-readable key supported by your service setup. The example service cannot prompt to unlock a key.

Run the test suite first:

```bash
python3 -m unittest discover -s tests -v
```

Make the configured runtime/state directories writable by the chosen service account before manual checks. The example systemd unit creates them automatically when that unit starts; a standalone manual run needs its directories prepared separately. Run manual commands as the same account that will own the controller and its key.

Probe without BMC writes:

```bash
python3 nvidia_idrac_fan.py --config /etc/nvidia-idrac-fan/config.json --probe
```

A successful probe reports the advertised fan limits and which memory sensors are available. A failed probe is a compatibility/configuration failure; do not bypass it. The probe and dry-run modes may create a local lock directory/file, but they do not write BMC settings.

Observe dry-run decisions, then stop with Ctrl-C:

```bash
python3 nvidia_idrac_fan.py --config /etc/nvidia-idrac-fan/config.json
```

Dry-run is the default. It does not provide cooling protection or change the current floor. Once you have management access and a recovery plan, explicitly enable writes:

```bash
python3 nvidia_idrac_fan.py --config /etc/nvidia-idrac-fan/config.json --apply
```

Do not use `--once --apply` to leave a lower floor: every normal exit requests 100%. To make a single explicit maximum request:

```bash
python3 nvidia_idrac_fan.py --config /etc/nvidia-idrac-fan/config.json --apply --failsafe
```

This also needs a valid capability probe and an available BMC. It is not a way to override unsupported firmware.

## Optional systemd service

`systemd/nvidia-idrac-fan.service` is an example, not an automatic installer. Review it, create its `nvidia-fan` account, and arrange NVIDIA device access for that account. The `video` supplementary group may not be sufficient on every distribution; verify access using the service account.

Install reviewed source under `/usr/local/lib/nvidia-idrac-fan/`, root-owned and not writable by the service account. Put configuration, identity, and verified host keys under `/etc/nvidia-idrac-fan/` with restrictive ownership. The example identity should be owned/readable only by `nvidia-fan`; configuration can be root-owned, group-readable by that account. Protect the parent directories against untrusted writes. Never copy somebody else's key or real configuration from an issue or repository.

The service creates private runtime/state directories, restarts after failures, and attempts a separate maximum-floor request after stopping. Vendor automatic cooling must remain enabled while the process is absent. For a normal maintenance stop, verify independently that 100% was accepted; if management access is down, systemd cannot make that guarantee.

After installing the reviewed unit:

```bash
sudo systemd-analyze verify /etc/systemd/system/nvidia-idrac-fan.service
sudo systemctl daemon-reload
sudo systemctl enable --now nvidia-idrac-fan.service
sudo systemctl status nvidia-idrac-fan.service
sudo journalctl -u nvidia-idrac-fan.service -f
```

Status is written atomically to the configured `status_file` with mode 0600. It contains temperatures, power/utilization, requested/verified floor, and fault information, without GPU UUIDs, passwords, private keys, or raw SSH output.

The lock prevents multiple copies using **the same lock path on one Linux host**. It does not coordinate multiple guests or different administrators. Run only one owner for a BMC.

## Optional physical diagnostics

Version 0.2.0 reads `racadm getsensorinfo` after control action, at most once per configured diagnostic interval (30 seconds by default). The same response can provide per-fan RPM, reported PWM percentage and health, plus available inlet, exhaust and CPU temperature readings. It does not assume six fans or use thresholds from another server. Unsupported fields remain `null`; malformed, duplicate, missing or stale readings cannot establish a positive proof. A diagnostic read failure does not change the fan policy, stop workloads, or convert a successful floor-setting transaction into a control fault. The diagnostic command still has its normal bounded SSH timeout.

The parser supports familiar iDRAC text rows such as `System Board Fan1 Ok 10440RPM ... 55%` and `CPU1 Temp Ok 44C ...`. Different formats may be unsupported. Raw output and arbitrary sensor names are not included in status.

`physical_diagnostics` includes an observation timestamp, monotonic age, maximum permitted age, freshness, per-fan readings, temperature readings, and three distinct checks. Observation age starts before the sensor request, so a slow response cannot make old evidence look newly sampled:

- `inventory_complete_verified`: all explicitly configured fan IDs were observed without malformed or duplicate fan rows. With automatic discovery alone, completeness is unknown and this check stays false.
- `fan_pwm_verified`: all configured fans are healthy and report PWM at least as high as the verified floor. It stays `null` without a configured complete inventory, a floor-setting proof, or fresh supported readings.
- `fan_rpm_verified`: every configured fan meets an explicitly approved, per-fan RPM envelope at its observed PWM. It stays `null` without calibration or below the calibrated range. Nonzero RPM only shows rotation; it is never treated as sufficient cooling.

Set `diagnostics.expected_fan_ids` to the exact IDs independently confirmed on your server to enable whole-inventory checks. The example leaves it `null` and does not claim complete inventory. `max_age_seconds` must be at least `interval_seconds`.

RPM calibration is optional and operator supplied. `rpm_minimums` maps each configured ID (as a string) to increasing `[PWM_percent, minimum_RPM]` points; it must cover exactly the configured IDs, use nondecreasing RPM minimums, and requires `rpm_calibration_approved=true`. At each observed PWM, the largest minimum for points at or below that PWM is used. No interpolation, automatic learning, or universal RPM minimum is supplied. Establish conservative acceptance envelopes with the correct fan hardware and sustained cooling tests before approving them. An RPM envelope proves neither heatsink airflow nor GPU thermal margin. Physical results are observational only and do not automatically alter control.

Disable these reads with `diagnostics.enabled=false` if the format is unsupported or the extra management traffic is undesirable. Core/HBM safety telemetry and the required thermal-setting capability/readback checks remain active.

## Cached status and Prometheus output

`status_metrics.py` reads one existing local status file. It imports no fan-control module, executes no commands, opens no network connection, and never reads NVIDIA or contacts iDRAC. It can read this package's schema 1 and the explicitly supported production status shape (`utc`, `monotonic`, `gpus`, `requested_floor_pct`, and optional `cooling_diagnostics.schema=1`). Legacy production fan measurements can be displayed but do not acquire fabricated PWM/RPM verification.

```bash
python3 status_metrics.py --status-file /var/lib/nvidia-idrac-fan/status.json
python3 status_metrics.py --status-file /var/lib/nvidia-idrac-fan/status.json --format json --max-age-seconds 90
```

Prometheus text is the default. Use a separate, reviewed node-exporter textfile collector or your monitoring agent to publish the output; this package opens no HTTP listener. Arrange private status-file read access without giving the collector SSH keys or NVIDIA device access. If saving output periodically, replace the `.prom` file atomically so scrapes cannot read partial content.

Exit status is **0** for a fresh readable snapshot, **1** for a stale readable snapshot, and **2** for unavailable, malformed, or unsupported status. These codes describe freshness, not thermal health: monitor the `fault`, `controller_running`, floor, physical freshness, and verification metrics too. `*_verified_known=0` denotes an unknown physical proof; an omitted measurement is not zero. Cached measurements remain available with their age after they become stale, but positive verification is invalidated.

On the same Linux boot, freshness uses the status monotonic sample time and current boot ID, so a wall-clock correction cannot make a stale sample fresh. A previous-boot snapshot is stale. Physical proof also expires at its source's `max_age_seconds`, even if the exporter's global maximum is longer. When boot metadata is absent, portable fallback uses wall time and rejects future timestamps; monitor clock synchronization if copying status between machines. `boot_id`, `sample_monotonic`, `cycle_sequence`, and a random per-invocation `producer_instance` are added to the private controller status for independent heartbeat monitoring. The invocation identity lets a monitor recognize a same-boot controller restart rather than confusing its restarted sequence with stale data. These fields describe controller status publication, not a new GPU measurement or physical fan read.

Only fixed metric names, numeric GPU/fan indexes, fixed temperature categories and finite numeric values are exported. Hostnames, addresses, usernames, paths, boot/invocation IDs, GPU UUIDs, raw errors and free-form reason strings are excluded. Missing/invalid status returns only fixed availability/freshness values.

## Hardware acceptance checklist

Before unattended operation, independently verify:

- Probing succeeds and unsupported/disabled capabilities fail clearly.
- Core and memory readings match trusted tools on every intended GPU; a missing sensor/device raises the floor.
- Every accepted floor changes as expected and all physical fan RPMs/airflow remain adequate.
- Diagnostic fan IDs match independently verified hardware; PWM is distinct from RPM, and any approved per-fan RPM envelope is appropriate at every intended duty.
- Missing/stale diagnostic readings are shown as unknown and do not masquerade as fresh physical proof.
- The vendor algorithm can increase fan speeds above the floor.
- Both GPUs cool safely during sustained simultaneous load at the worst expected inlet temperature.
- Telemetry loss, invalid responses, iDRAC connectivity loss and return, controller exit/restart, and host reboot produce the documented behavior.
- No other controller or scheduled job writes fan settings.

This repository's offline tests do not establish any of these live-hardware gates. Report actual server/iDRAC/firmware/GPU versions and evidence when contributing compatibility results.

## Rollback

Record the original `MinimumFanSpeed` and the untouched thermal settings before enabling writes. Stop the service; it requests 100% and does not automatically restore a lower value. Confirm workloads and temperatures are safe, then restore the original floor using your normal trusted administrator interface. Dell documents 255 as an unset user minimum on supported iDRAC8 firmware; **do not assume 255 is accepted on another platform**. Never disable vendor thermal response as a rollback shortcut.

No private network addresses, local user directories, actual hostnames, account credentials, deployment keys, or model/service configuration are included in the repository.

## Sources

- [GitHub comparison and improvement plan](UPSTREAM_REVIEW.md) — pinned source review, current gaps, proposed monitoring/RPM/redundancy work, and required validation.
- [Dell iDRAC8: modifying thermal settings with RACADM](https://www.dell.com/support/manuals/en-uk/idrac8-lifecycle-controller-v2.75.75.75/idrac8_2.75.75.75_ug/modifying-thermal-settings-using-racadm?guid=guid-476e0462-fb31-4dac-be4a-3af3801ae556&lang=en-us) — supported settings, advertised limits, minimum-floor behavior, and PCI response.
- [Dell iDRAC8: modifying thermal settings](https://www.dell.com/support/manuals/en-us/integrated-dell-remote-access-cntrllr-8-with-lifecycle-controller-v2.00.00.00/idrac8_ug_pub-v1/modifying-thermal-settings-using-idrac-web-interface?guid=guid-c1a01b25-ea28-407e-b60d-a5b469bf42d4&lang=en-us) — persistent settings and platform-dependent support.
- [NVIDIA System Management Interface](https://docs.nvidia.com/deploy/nvidia-smi/) — XML telemetry, Celsius temperatures, power/utilization and device-dependent memory temperature support.

MIT licensed. Not affiliated with or endorsed by Dell or NVIDIA.
