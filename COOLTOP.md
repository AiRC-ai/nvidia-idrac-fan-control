# cooltop

A read-only terminal dashboard for the NVIDIA GPU and iDRAC cooling telemetry. It shows live measurements and rolling history in the style of `htop` and `nvtop`.

Version 0.3.0 opens with a summary-first overview: prominent GPU temperatures, load, power and VRAM readings; compact thermal and activity histories; and measured fan feedback beside cooling status. Wider windows use three columns. Neutral title bands separate panels, while core, HBM and power retain consistent colors. A separate detail view provides the larger individual charts.

The dashboard is separate from the fan controller. Starting, pausing, or quitting it does not change fans, power limits, models, or services. It never opens a BMC connection. NVIDIA's optional local telemetry query supplies device names, VRAM, power limits, and clocks; it does not run a GPU workload.

The original deployment uses a Dell PowerEdge R730 with iDRAC8, two Tesla V100 PCIe 32 GB cards, and a Debian GPU guest on ESXi. The display is observational: fan rotation and temperatures do not certify sufficient heatsink airflow.

Cooling panels currently understand the original production watchdog and independent guard JSON formats. They are not an adapter for every fan-control project or for this repository's generic controller schema. The default inputs are `/var/lib/airc-gpu-fan-watchdog/status.json` and `/run/airc-gpu-thermal-guard/guard.json`; override them with `--status-file` and `--guard-file` when needed. NVIDIA readings remain useful independently of those inputs; unavailable cooling fields stay unconfirmed.

## Run

```bash
sudo cooltop
```

Run inside a terminal or an SSH session with a terminal allocated. A UTF-8 locale and a 256-color terminal give the best display. Use `cooltop --help` for input paths, refresh options, and a plain-text snapshot suitable for logging. The two telemetry JSON files in the original deployment are root-readable; the dashboard does not change their permissions.

| Key | Action |
|---|---|
| `q` or Escape | Quit the dashboard |
| `p` or Space | Pause/resume the display |
| `h` or `?` | Show/hide help |
| `v` or Tab | Switch between overview and detail views |
| `+` / `-` | Select 1, 5, or 15 minutes of history |
| `r` | Refresh local readings |

```bash
sudo cooltop --snapshot
sudo cooltop --history 60
sudo cooltop --view detail
sudo cooltop --ascii --no-gpu-query
```

The default display refreshes once per second; the optional NVIDIA query runs every five seconds with a two-second command timeout. `--no-gpu-query` disables that query entirely. The dashboard displays the first two GPUs and up to six fans; larger GPU inventories are marked. The overview prioritizes current readings and fan feedback, bounds individual chart height, and adapts to two- or three-column arrangements. Tall windows add GPU memory residency, host memory and CPU histories, followed by fan-control and chassis-temperature detail when space permits. Detail view allocates more height to separate temperature, utilization and power charts. A 160×60 terminal gives a spacious view; 80×24 remains supported with compact panels. Select the initial view with `--view overview` or `--view detail`.

History begins when the dashboard starts. It is held in memory and discarded on exit. Trend charts use step-held cached readings only while their source remains valid; bins show the peak measurement so brief thermal/load spikes remain visible. Gaps indicate unavailable or expired telemetry. Stored measurements remain actual deduplicated observations. Requested fan floor and measured fan PWM/RPM are displayed separately. A loaded model can consume VRAM while GPU utilization is idle. Core and HBM share a thermal scale; dotted reference lines show the configured guard cutoffs. The current temperature values warn as they approach those limits. Selected-window ranges use separate minimum and maximum aggregation. Power percentages require a fresh, positive cap reading; stale caps remain marked in watts and are not used to normalize charts.

## Install manually

Python 3.10+ with `curses` is required. Debian's system Python provides this module. No Python packages need to be installed.

Install the two dashboard modules together in a root-owned directory, then link the executable:

```bash
sudo install -d -m 0755 /usr/local/lib/cooltop
sudo install -m 0755 cooltop.py /usr/local/lib/cooltop/cooltop.py
sudo install -m 0644 cooltop_data.py /usr/local/lib/cooltop/cooltop_data.py
sudo ln -s /usr/local/lib/cooltop/cooltop.py /usr/local/bin/cooltop
```

Check that the command and destination do not already contain another installation before applying these commands. Uninstall only the files installed above; no controller or service change is needed.

The display reads existing telemetry. It does not install or start a fan controller, thermal guard, or inference engine. Unavailable inputs are reported in the dashboard.
