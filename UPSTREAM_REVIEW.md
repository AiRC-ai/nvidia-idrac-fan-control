# GitHub comparison and improvement plan

Reviewed November 7, 2026. This is a source review, not a benchmark or hardware compatibility certification. The links below pin the inspected revisions. No upstream controller was installed and no live thermal settings were changed for this research.

The comparison concerns a Dell PowerEdge R730 / iDRAC8 with two Tesla V100 PCIe 32 GB GPUs in a Debian guest on ESXi 7. The useful next steps are stronger physical fan verification, clearer recovery diagnostics, and protection outside the GPU guest. A replacement fan curve alone would not address those gaps.

## Keep the two implementations distinct

The original installation and this public package are separate implementations. Results from one do not validate the other.

| Area | Original installation inspected | This public package at its initial release |
|---|---|---|
| GPU inputs | Both GPUs' core/HBM temperatures, power, utilization and individual temperature trends | All configured GPUs' core/memory temperatures, power and utilization; no temperature trend predictor |
| Control | RACADM automatic minimum floor; PCI response changes with the existing idle/active policy | RACADM automatic minimum floor only; requires PCI response already enabled and never changes it |
| Control failure | Persistent degraded state requests 100%, retries and preserves inference while independent GPU protection is healthy | Requests 100% and retries; never controls workload execution |
| Verification | Setting readback plus all six fan health, RPM and reported percentage records | Setting acknowledgement/readback only; physical RPM is not verified |
| Recovery | Serialized, bounded management operations; verified recovery before dynamic control resumes | Single configured SSH endpoint, bounded calls and verified setting readback |
| Workload protection | Separate GPU thermal guard in the same guest | No workload-stop integration |
| Evidence | 79 offline regression tests; deployed recovery and a short streamed inference acceptance | 38 offline tests; read-only R730 capability probe; continuous control remains unvalidated |

Neither uses raw IPMI manual fan mode. Keep Dell's automatic thermal control and the highest individual GPU/HBM cooling demand. A cold GPU must never average away a hot GPU.

## Source comparison

| Project and pinned source | Useful implementation | Adaptation needed here |
|---|---|---|
| [ST-DEVPT/rack-fan-control: fan response](https://github.com/ST-DEVPT/rack-fan-control/blob/f5fb568c61a3ff0355d176d3957850794207a3fb/fanctl/server.py#L768-L829) | Compares observed RPM with an expected response after settling; qualifies repeated discrepancies. | Its comparison uses average RPM and an exact-speed backend. Our floor controller needs individual fan lower bounds based on actual reported duty. Higher speed from Dell automatic control is allowed. |
| [ST-DEVPT/rack-fan-control: metrics](https://github.com/ST-DEVPT/rack-fan-control/blob/f5fb568c61a3ff0355d176d3957850794207a3fb/fanctl/metrics.py#L27-L72), [external guard](https://github.com/ST-DEVPT/rack-fan-control/blob/f5fb568c61a3ff0355d176d3957850794207a3fb/scripts/fan-guard.sh#L29-L103) | Exports loop/update freshness, control errors and fan measurements; adds a watcher outside the controller's container. | Use existing cached state for monitoring. Its Docker host script and automatic-mode handoff are architectural references, not an ESXi installation procedure. |
| [mstrhakr/Dell_iDRAC_fan_controller_Docker: temperature collection](https://github.com/mstrhakr/Dell_iDRAC_fan_controller_Docker/blob/af9f04c78e34d40c252046e1d8a9ac4697a75947/cmd/controller/temperature.go#L15-L70), [PID](https://github.com/mstrhakr/Dell_iDRAC_fan_controller_Docker/blob/af9f04c78e34d40c252046e1d8a9ac4697a75947/cmd/controller/pid.go#L27-L119), [trend](https://github.com/mstrhakr/Dell_iDRAC_fan_controller_Docker/blob/af9f04c78e34d40c252046e1d8a9ac4697a75947/cmd/controller/trend.go#L34-L72) | Collects inlet/exhaust and cached fan RPM; scales corrections by elapsed time, bounds accumulated corrections and resets trend history after observation gaps. | Cached RPM can survive a failed refresh, so attach explicit validity/age. Do not transplant its temperature targets or fan ceilings. The original installation already handles per-GPU trends and observation gaps. |
| [greghughespdx/dell-poweredge-fan-control](https://github.com/greghughespdx/dell-poweredge-fan-control/blob/2e0a4162ff7a8ad0a7eb0717dec0cdddd4d2738f/ipmi_fan_control.py#L167-L209) | Inlet-based demand with immediate increases and gradual reductions; [combines demands by taking the maximum](https://github.com/greghughespdx/dell-poweredge-fan-control/blob/2e0a4162ff7a8ad0a7eb0717dec0cdddd4d2738f/ipmi_fan_control.py#L468-L493). | Missing temperature inputs default to zero in the inspected reader. Our adaptation must retain an unavailable state instead. Its raw IPMI actuator and numerical curve are not drop-in replacements. |
| [kuan909608/dell-idrac-fan-controller-gpu](https://github.com/kuan909608/dell-idrac-fan-controller-gpu/blob/7f5ccdbd64eb711a31217703540520036247c8ce/control_policy.py#L8-L72) | Immutable sensor/decision records and a pure policy function make missing-source behavior testable. | It offers maximum or average aggregation. Retain maximum individual demand here, with separate core and HBM limits. Missing configured sources must remain explicitly unhealthy. |
| [sanxiago/fan-noise-guard](https://github.com/sanxiago/fan-noise-guard/blob/613be313a70645013efcaaae5b4b24b865b08905/fan-noise-guard.sh#L138-L174) | Separates auxiliary diagnostic readings from protection inputs. | Reads the first GPU only. Its [RPM check](https://github.com/sanxiago/fan-noise-guard/blob/613be313a70645013efcaaae5b4b24b865b08905/fan-noise-guard.sh#L282-L295) is an average-RPM, unexpectedly-high-speed diagnostic; it does not establish a minimum response for every fan. |
| [craigspaterson/dell-r730xd-fan-control](https://github.com/craigspaterson/dell-r730xd-fan-control/blob/4ed78675af14b87de30fed756416e22ea0cb02d0/fan_control.py#L146-L157) | A closely related R730 NVIDIA fan-control example. | Its GPU reader converts the complete command output into one integer. Multiple GPU result lines fail that parse; the inspected reader is unsuitable for this dual-GPU setup without changes. |

This review borrows design ideas, not source code. Check each project's license before incorporating implementation. Similar server names or a successful SSH connection do not establish equivalent fan commands, sensors or firmware behavior.

## Proposed work, in order

### 1. Publish cooling proof and recovery state

Add a read-only status/exporter layer over the controller's existing atomic state. Dashboard requests must not open SSH sessions or poll iDRAC themselves. Separate these observations:

- Requested floor and acknowledged/read-back floor.
- Reported fan duty and measured RPM for every fan.
- Age and validity of each proof, plus controller-loop freshness.
- Each GPU's core/HBM temperature, power, activity and thermal headroom.
- Current management route, outage duration, reconnect count and command latency, using non-sensitive labels.
- The reason for a high fan floor: load, heat, rising temperature, startup, stale telemetry or management degradation.

Alert on a sustained fault and on recovery, rather than emitting a notification on every retry. Keep a healthy controller loop distinct from reachable management hardware. Start with local/private access; do not expose unauthenticated operational data publicly.

**Acceptance:** metrics still respond when iDRAC is unreachable; stale data is marked stale; exporting adds no management sessions; logs and labels reveal no credentials, raw command output or private endpoints.

### 2. Verify each fan's physical response

The original installation currently accepts six healthy fan records with at least 360 RPM and verifies their reported percentage against the requested floor. That detects stalls and missing records, but a slowly turning, non-stalled fan could pass. The generic package does not yet perform even this physical check.

Build an approved per-fan RPM envelope from known-good hardware at already-approved operating levels. Record fan identity/model, reported actual percentage, settling time and sample age. Match against actual duty where available: the requested setting is only a floor, and Dell may legitimately command more cooling. Use a conservative lower bound, not an exact-RPM equality or a chassis average. Never learn a progressively failing fan as the new normal.

Begin in diagnostic-only mode. Qualify persistent underspeed after a settling interval; then request 100% and retry. A communication failure or RPM discrepancy alone must not acquire an inference-stop action. Preserve the original independent thermal protection. RPM is evidence of rotation, not a measurement of heatsink airflow.

**Acceptance:** one weak fan cannot hide behind five fast fans; stale/missing records cannot verify cooling; normal settling and firmware speed increases do not create false faults; no calibration step lowers an existing approved floor.

### 3. Add a watcher outside the GPU guest

The original independent GPU guard and fan controller still share a VM. A guest crash or kernel failure can remove both. A separately hosted watcher can cover part of that failure domain.

Use a qualified missing heartbeat to request a persistent 100% floor through an independently reachable management path. Define ownership and recovery acknowledgement so the guest cannot lower the floor while the external watcher still owns emergency control. Serialize commands, budget iDRAC sessions, bound retries, and use strict host identity checks. Only release emergency ownership after fresh recovery proof.

This needs an ESXi-appropriate design or another management host; the upstream Docker guard cannot simply be installed unchanged. A second process inside the same guest does not provide this redundancy. If iDRAC and every management path are unavailable, no watcher can guarantee a new fan command was accepted.

**Acceptance:** guest/controller death, slow startup, stale heartbeat, external watcher restart and simultaneous recovery do not produce competing fan writers. Normal recovery restores dynamic control automatically.

### 4. Measure ambient conditions before tuning for them

Record inlet, exhaust and CPU temperatures alongside both GPUs and the individual fan measurements. This would help distinguish warm garage air from a fan problem or uneven GPU airflow.

First run any inlet/power prediction in shadow mode: record the proposed decision without applying it. A later ambient term should initially only raise the existing GPU-selected floor. A cool inlet or exhaust reading cannot cancel hot HBM demand. Unsupported or missing sensors stay unavailable, never zero. Add measurements to existing bounded chassis collection where supported rather than creating a dashboard polling loop.

Add a diagnostic for **cooling saturated**: maximum fan output is freshly verified, but temperatures keep rising or the GPU reports thermal slowdown. This suggests inadequate effective cooling; it does not prove a defective GPU.

**Acceptance:** compare predictions with actual temperature slopes across steady load, rapid load changes and warmer inlet conditions. Do not copy another chassis's numeric fan curve or automatically lower the current active floor on the strength of seasonal weather.

### 5. Extend NVIDIA diagnostics and the generic policy carefully

Capability-aware throttle reasons and device-reported temperature limits would help explain slowdowns. NVIDIA distinguishes several thermal/power conditions and device-dependent telemetry; unavailable fields must stay unavailable. Its `T.Limit` value is a relative temperature margin, not an absolute Celsius reading. These additional diagnostics would not relax the configured thermal cutoffs. [NVIDIA SMI documentation](https://docs.nvidia.com/deploy/nvidia-smi/)

A persistent NVML reader may reduce repeated command startup overhead, but measure that benefit before adding a dependency. Preserve independence between workload thermal protection and the main controller; a shared collector can introduce a common failure or stale-data source.

Port the original installation's per-GPU trend and observation-gap tests into the public package before adding a predictor. Use elapsed monotonic time, invalidate discontinuous history, and let raw hot readings override smoothing. A more elaborate PID loop is not required to fix connectivity or prove airflow.

## Validation boundary

All work above is proposed. The research itself changed no live fan curve, power cap, cutoff, model configuration or inference workload. No sustained-load, physical-link-disconnection or reboot test was performed for this comparison.

Implement monitoring first, then validate physical fan response, then the external watcher, and finally measured ambient prediction. Keep offline fault-injection tests separate from hardware acceptance. Record the exact artifact, firmware, sensor capabilities and observed recovery at each stage. Consult [VALIDATION.md](VALIDATION.md) for this package's current evidence and remaining hardware gates.
