# Validation status

Reviewed 2026-11-08T04:10:51.115293+00:00.

- Version: `0.1.0`.
- Python offline suite: **38 tests passed** using Python 3.9.6.
- Coverage: synthetic NVIDIA/RACADM parsing, mandatory/optional sensors, identity/count changes, thermal and activity demand, stepwise cooldown, control-loss maximum requests, recovery, strict SSH/configuration validation, path-alias rejection, atomic status, and successful/failed final maximum requests.
- Independent offline source review accepted the fixes for status/lock/credential path aliasing, OpenSSH filename parsing, and final-shutdown exit status.
- Source compilation and `--version` smoke check passed.
- Privacy scan found no private addresses, real deployment hostnames, user/workspace paths, credentials, actual GPU identifiers, or copied model/service configuration.

The 38 offline tests contacted no hardware. A separate **read-only live probe** then passed on the R730/iDRAC8 through strict, key-authenticated SSH: two V100s, memory-temperature readings on both, advertised fan-floor limits 0–100%, PCI fan response enabled. It made no BMC writes and started no GPU workload. Private probe configuration is stored outside this repository.

**Not yet accepted:** continuous write/control behavior of this generic package, physical fan RPM/airflow, sustained-load and inlet-temperature margin, systemd execution, restart/reboot behavior, and compatibility beyond the documented policy-development environment. See the README's hardware acceptance checklist.
