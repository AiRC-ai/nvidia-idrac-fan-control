# Independent external heartbeat guard

`external_guard.py` provides a **read-only monitor**, run on a separate Linux or macOS management host, for the NVIDIA/iDRAC Linux guest controller. It can detect a stopped controller, failed floor readback, a controller-reported fault, stale status, or an unreachable guest. It produces machine-readable local alarms and recovery receipts. It has no NVIDIA, BMC, RACADM, systemctl, workload-stop, or power-control command.

This is implemented monitoring, **not a second cooling actuator or a high-availability safety controller**. No external BMC failover is implemented. There is deliberately no `--apply` mode and no iDRAC credential mapping. The original guest remains the only fan-setting owner. Neither a heartbeat nor a verified floor proves fan RPM, airflow, or safe temperatures.

The package is experimental. Offline tests and limited integration are recorded in [VALIDATION.md](VALIDATION.md); neither validates a second cooling actuator. Alert delivery, actual guest/network/power failure, and reboot acceptance remain outstanding. No dedicated restricted SSH account was created by these checks.

## Why it does not write 100%

An external `MinimumFanSpeed=100` command can be overwritten seconds later by a still-running guest controller. Losing the SSH path to a guest does not prove that the guest has stopped or lost BMC access. A local or management-host lock does not prevent that split-brain case.

A safe external writer needs a protocol that the guest controller actually enforces: a shared ownership epoch/lease or fencing mechanism, a bounded loss-of-lease response that prevents the guest lowering fans, and independent readback/physical validation. Merely creating an external lease file or repeatedly writing 100% does not supply those properties. This package does neither. Defer automatic external actuation until such a protocol has been designed, reviewed, and tested through network partitions, VM stalls, management-host failure/reboot, and recovery. Keep vendor cooling and hardware thermal protections enabled.

## Components and protocol

Install the same reviewed `external_guard.py` on both systems:

- On the **GPU guest**, its `heartbeat` subcommand only reads the controller's private status file and Linux boot identity. It does not poll GPUs, invoke subprocesses, or write any file.
- On the **independent management host**, its `monitor` subcommand invokes that fixed helper over strict key SSH. It writes only its own private lock, state, and status files and its journal output.

The default `generic` helper format requires status schema1 with `boot_id`, `sample_monotonic`, `cycle_sequence`, `controller_running`, `dry_run`, `fault`, `floor_setting_verified`, and `verified_floor_pct`. It also checks `control_verified` if present. Deploy the accompanying version of `nvidia_idrac_fan.py` that produces these fields. Older status without boot/monotonic fields is incompatible and never reported healthy.

An explicit `production` adapter supports a second cached-status contract: `utc` (timezone-aware ISO8601), `boot_id`, `monotonic`, `invocation_id`, `cycle_sequence`, `controller_running`, `control_verified`, `control_degraded`, `fault_latched`, `requested_floor_pct`, and `gpus` (typed `gpu`, `core_c`, `hbm_c`, `power_w`, and `util_pct` fields). Select it with `remote_status_format: "production"` and the matching guest helper `--status-format production`. There is no automatic shape detection. Legacy production data lacking the new running/sequence fields is rejected. A degraded controller is unhealthy even if its100% floor was read back successfully. Empty GPU data is allowed for an explicitly stopped/faulted/degraded/unverified controller, and cannot establish healthy operation. Invocation identity is fingerprinted; private endpoints, row data, reasons, and other source fields are not exported. This adapter is a data-format integration, not proof of that installation's behavior.

The status boot identity must equal the guest's current Linux boot identity. Age is computed from the **guest's own monotonic clock**, then sent with a sanitized heartbeat protocol1 response. Guest and management-host wall clocks need not agree. Prior-boot data, future monotonic timestamps, malformed/duplicate JSON fields, inconsistent ages, wrong types, and missing required fields fail closed to an unhealthy observation. GPU UUIDs and raw telemetry do not leave the guest through this helper. Private monitor state stores boot/producer identity, the latest advancing cycle and monotonic watermark, qualification time, and alarm/recovery counters. These identities are omitted from monitor receipts. The management-host boot identity comes from Linux's boot-id file or the fixed, five-second-bounded macOS command `/usr/sbin/sysctl -n kern.bootsessionuuid`; its UUID is validated and never printed in monitor receipts. The guest helper remains Linux-only and does not call that command.

## Qualification and recovery

With the example configuration:

- A missing, invalid, or unreachable heartbeat starts qualification. A stale heartbeat is one older than120 seconds. If unhealthy observations continue for30 seconds, the monitor raises `alert=true`.
- A guest that explicitly reports `controller_running=false`, `dry_run=true`, `fault=true`, `control_degraded=true`, or an unverified floor raises an immediate alarm. A setting readback can be verified while the guest reports a thermal fault; the fault still wins.
- An alarm remains active until three fresh, healthy controller cycles with strictly advancing sequence numbers and monotonic timestamps have been observed from one producer. Re-reading the same status cannot count again; regressed or inconsistently advanced samples are unhealthy and cannot lower the retained watermark. A failed recovery observation resets the success count. A changed guest boot or producer identity starts a new recovery sequence. Generic producers should supply the accompanying controller's per-invocation `producer_instance` fingerprint so a same-boot controller restart can be recognized safely.
- Transient unqualified failures clear on a good observation. Qualified alerts persist through monitor-process restarts and management-host reboots. Monotonic qualification duration survives process restart on the same management-host boot. Linux uses its system-wide `time.monotonic()`; macOS explicitly uses `time.clock_gettime(time.CLOCK_MONOTONIC)` because Python3.9's macOS `time.monotonic()` can have a process-relative origin. The management clock is validated and never falls back to wall time. State carries the clock domain `management-system-monotonic-v1`. A new boot, missing legacy clock domain, or changed domain resets incompatible qualification/recovery timing while retaining any qualified alarm; new observations must establish recovery. The guest helper continues using its own Linux monotonic clock.
- Polling is not real-time. The default SSH operation is bounded to15 seconds, with10 seconds added between cycles. Detection includes both the freshness threshold and qualification/polling delay. Increase the freshness threshold if normal controller cycles can legitimately exceed it; do not mistake the threshold for a hardware cooling guarantee.

Status has `phase` equal to `healthy`, `qualifying`, `alert`, or `recovering`. `bmc_write_attempted` and `hardware_cooling_verified` are always false. A graceful monitor exit writes `monitor_running=false`; it does not clear a pending alarm or change fan settings. Check status freshness as well as its last recorded phase when integrating it with another monitor. Standard output is limited to the first observation and phase/reason/alarm transitions; the atomic status file still updates every cycle.

## Configure without exposing credentials

Review `systemd/nvidia-idrac-external-guard.config.example.json`. All endpoints and paths in it are generic placeholders. Copy it to a private management-host configuration and provide your own guest endpoint, separate heartbeat SSH key, independently verified guest host key, and private local paths. No BMC key or password belongs in this configuration.

Protect source/configuration/SSH files and parent directories against untrusted writes. Local paths for configuration, key, trust, state, status, and lock must be distinct; aliases are rejected. Key mode must be0600 or stricter, with a trusted owner; the trust file must have content and no group/world write access. SSH filename expansion/list characters and remote shell metacharacters are rejected. The transport uses `-F /dev/null`, strict host-key checks, a specific identity/trust file, no password/interactive authentication, and a fixed helper command. Verify the host fingerprint through an independent trusted path. An unverified `ssh-keyscan` result is not sufficient.

The guest helper needs permission to read the private controller status, and accepts files owned by the helper account or root. One option is a dedicated restricted SSH key for the existing controller account, with a forced command invoking exactly the read-only helper:

```text
restrict,command="/usr/bin/python3 /usr/local/lib/nvidia-idrac-fan/external_guard.py heartbeat --status-file /var/lib/nvidia-idrac-fan/status.json" ssh-ed25519 YOUR_PUBLIC_HEARTBEAT_KEY
```

This is an illustrative authorized-key line, not an installer. Review the target OpenSSH version's `restrict` support and account shell requirements. Keep source, `authorized_keys`, and their parent directories protected. Never grant an unrestricted shell or reuse the iDRAC deployment key just to make monitoring work. A separate account needs an explicitly reviewed read-only mechanism for the controller's mode0600 status; do not weaken that file to world-readable. SSH permissions and forced-command restrictions require independent acceptance on the actual guest.

The management-host configuration must point to the same helper, status path, and format as the forced command. Append `--status-format production` to the forced command only for that explicit adapter. A copied status file or a command that echoes static JSON is not a working heartbeat source.

## Offline tests and manual checks

Run offline tests before configuring any endpoint:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

On the guest, using the intended restricted account and real status path, the helper reads local state only:

```bash
python3 external_guard.py heartbeat --status-file /var/lib/nvidia-idrac-fan/status.json
```

On the separate management host, a single observation performs read-only SSH and writes private local receipts:

```bash
python3 external_guard.py monitor --config /etc/nvidia-idrac-external-guard/config.json --once
```

Exit codes are0 for healthy,1 for an active alarm or local configuration/state failure, and2 for a still-qualifying unhealthy observation. `recovering` still returns1. Repeated `--once` checks retain qualification and recovery state. Do not delete the private state file as an automatic recovery action: that could erase a qualified alarm. An invalid state file is an explicit local error, not silently reset.

To observe continuously, omit `--once`. JSON goes to stdout/journald and the configured status file. No email, chat, external webhook, paging, or notification integration is automatically installed. Connect the receipt to your existing monitoring system and separately test actual alert delivery; the program itself only records the alarm.

## Optional management-host systemd service

`systemd/nvidia-idrac-external-guard.service` is an example. It belongs on the independent management host, **not another VM on the same ESXi machine** if the goal is independence from that host's failure. Consider independent power, networking, and management access too.

Review and create its `nvidia-guard` account, place source root-owned under `/usr/local/lib/nvidia-idrac-fan/`, and arrange private account-readable configuration and SSH files under `/etc/nvidia-idrac-external-guard/`. The example creates private `/var/lib/nvidia-idrac-external-guard/` and `/run/nvidia-idrac-external-guard/` directories. Its60-second watchdog exceeds the example15-second SSH timeout plus10-second polling delay and reserve. The program rejects an inadequate watchdog budget. Change the unit's budget if configuring larger timeouts. `READY=1` means the monitor process started, not that its guest is healthy. The watchdog is fed while a known guest alarm is being monitored; it detects a hung monitor process, not a resolved cooling incident.

After review, verify the unit locally and enable it using the administrator's normal service workflow. Restarting the monitor does not restart the guest, replay inference, clear faults, or touch BMC settings. Uninstall by stopping/disabling this external service and removing only its reviewed files and dedicated heartbeat key; it has no fan-setting rollback because it never changes one.

On a macOS management host, use the same `monitor --config PRIVATE_PATH` CLI under a separately reviewed launch agent/service instead of systemd. Provide actual absolute Python, source, state, lock, and SSH paths for that account; Linux example paths are not a macOS installer. Keep its state outside temporary directories so alerts survive a restart, supervise the process, and monitor receipt freshness/alert delivery independently. No launch agent is automatically installed by this package.

## Acceptance still required

Before relying on external monitoring, independently test a healthy live controller, stale producer file, controller graceful stop, missing/corrupt status, guest SSH denial and restoration, guest reboot, management-host process restart/reboot, three-cycle recovery, and alarm delivery. Verify no real credential or status file is copied into a public repository. A healthy response does not establish fan RPM, thermal headroom, or survivability of a frozen guest. No external writer or coordinated failover is accepted by this implementation.
