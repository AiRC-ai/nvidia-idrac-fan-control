# Security and safety

This software controls a persistent server cooling setting. Review changes and test failure cases before connecting it to hardware. It is an experimental supplementary controller, not a substitute for vendor automatic cooling or independent overtemperature protection.

The only writable RACADM property is `System.ThermalSettings.MinimumFanSpeed`, with an integer 1–100. The program requires enabled third-party PCI fan response and advertised fan limits, and verifies write acknowledgement plus readback. It never invokes a shell locally or accepts arbitrary remote commands in configuration.

Use dedicated accounts/keys, strict SSH host-key verification, least privilege, restrictive local file ownership, and a protected management network. Never publish private keys, passwords, real configuration, raw GPU XML, or unredacted SSH logs. Host-key changes require independent verification; do not bypass them to recover connectivity.

Configuration and executable files must be protected against untrusted writes. The service's local lock is not distributed: two guests can still fight over one BMC. An administrator must designate a single controller owner.

Loss of host power, the BMC, networking, NVIDIA telemetry, or the controller can prevent a fan-floor request from being applied. A read-back floor is not proof of actual airflow or cooling. Critical temperatures request maximum fans but do not stop workloads. Keep vendor thermal protection enabled and act on overheating independently.

For a security concern, use the repository's private security advisory facility if available. Do not place secrets in a public issue. Include a minimal, synthetic reproduction and the affected version.
