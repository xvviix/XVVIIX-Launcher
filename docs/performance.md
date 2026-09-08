# Performance and the 60 Hz Game Monitor overlay

> Historical notes for the preceding performance phase. The [current monitor revision](monitor-update.md) replaces the full process inventory with top-five selection, lowers opacity and adds compact network rates. The old measurements below are not claims about the newer process-table implementation.

This phase redesigns **only the floating overlay**, following the supplied compact reference. The main Monitor tab keeps its existing appearance. Its charts/process-table update path is optimized without a visual redesign.

![Actual Tk overlay using explicitly synthetic demonstration values](images/monitor-overlay.png)

[Watch the actual Tk animation demo](images/monitor-overlay-demo.mp4). The preview uses synthetic readings, clearly marked **DEMO · SAMPLE DATA**; it is not a recording of the user's computer or a game-FPS measurement.

## Open and use

- With the launcher focused, press **Ctrl + Alt + M** or use the Monitor controls to open the overlay.
- Drag the title area to move it.
- The small square button collapses/expands it. Compact mode keeps a short metrics line and stops animation work.
- **×** or **Escape** closes it.
- The default panel is **282 × 566 logical pixels**. Fonts/layout scale together for high-DPI displays.

The overlay is an ordinary always-on-top desktop window. **Borderless/windowed games are the intended use.** It does not inject into DirectX/Vulkan or bypass exclusive-fullscreen/anti-cheat restrictions, so it may not appear over an exclusive-fullscreen game.

## What “60 fps” means here

The expanded, visible overlay targets **60 drawing callbacks per second**. It reuses canvas objects and smoothly interpolates bars between genuine readings. The CPU history scrolls independently of sensor collection; its small graph auto-ranges for readability, while percentage bars always use the actual 0–100% range.

This is **not** 60 complete process scans per second, a claim about game FPS, or a vsync/presented-frame guarantee. Real results depend on Windows scheduling, display scaling, drivers and concurrent game/system load. The small `UI … fps` counter reports the recent drawing-callback rate.

| Work | Cadence / policy |
|---|---|
| Visible expanded-overlay drawing | 60 Hz target; missed deadlines are skipped, not queued |
| Overlay reading the latest cached summary | Every 500 ms |
| CPU/GPU/memory and I/O telemetry | Application service samples about once per second |
| Process inventory | Once every 2 seconds, including empty-result caching |
| CPU clock metadata | Cached for 2 seconds |
| System-volume capacity/usage | Cached for 5 seconds; I/O rates remain live samples |
| Slow interfaces, battery and CPU sensor metadata | Existing 8-second cache |
| Main Monitor tab | Existing 900 ms polling, only when visible |
| Hidden/collapsed overlay | Animation stopped; hidden overlays also stop polling |
| Neither Monitor view visible | Existing delayed telemetry shutdown |

On Windows, the frame clock requests a 1 ms multimedia-timer period only while visible animation is active. It releases that request on hide, collapse, close or drawing failure. This can cost some power while active; it is not left enabled globally for the entire launcher session. Failure to obtain it degrades normally rather than blocking startup.

Unsupported GPU/thermal readings remain `--`. Stale or failed telemetry is visibly unavailable, not extrapolated into fictitious sensor values. Top CPU/RAM entries use the current-user process inventory, with normalized percentages; the system-idle pseudo-process is not listed as an application consuming 1000% CPU.

## Reduced work

### Small overlay snapshots

The overlay receives only the metrics, short history and top-process summaries it displays. It no longer deep-copies up to 512 detailed process records, usernames, executable paths, interfaces and per-core arrays on each update.

Full Monitor snapshots remain defensive copies. Their copying now happens outside the publication lock, so the collector does not wait for the UI to copy a large table. The main tab skips unchanged process revisions and reuses unchanged row values/positions and chart items.

### Responsive artwork loading

Cold card artwork is prepared on **one lazy background worker**, with a bounded queue and deduplication. Page changes cancel queued work for old pages. Image-to-Tk conversion happens only on the Tk thread, and results update the individual card rather than rebuilding the whole page.

Cards can briefly display a dark placeholder while their artwork is being prepared. This removes a synchronous UI wait; it does **not** make decoding free or promise that every image will finish instantly. Large source images receive decoder-size hints and bounded intermediate resizing. Shared readability gradients and bounded image caches avoid repeated work.

### Other scheduling changes

- UI result dispatch uses a 4 ms slice, leaving headroom for animation.
- An empty library no longer triggers unnecessary full process enumeration; already tracked sessions are still accounted for.
- Hidden Monitor views do not keep redrawing tables/charts.
- No password derivation, encryption, backup-validation or data-integrity check was weakened. The storage, cryptography and record-normalization modules are unchanged by this phase.

## Measured local results

Environment: **Linux, Python 3.13.14, Tk 8.6, Xvfb 1280 × 900**, 1× panel scale. These are controlled development measurements, **not Windows/game-load certification**.

| Measurement | Observed result |
|---|---|
| Drawing-callback rate during a 12-second run | **60.00 fps** |
| Drawing callback p95 | **0.241 ms** |
| Frame interval p95 | **17.43 ms** |
| Missed frame deadlines in that run | **0** |
| Python-process CPU, one-core basis | **2.69%** |
| Data samples during the run | **12** synthetic samples, not 720 process scans |
| Canvas objects | **88**, retained rather than recreated per frame |

The Python CPU figure excludes other processes, including the Linux display server. It is not total system/GPU consumption.

For a synthetic 512-process snapshot, median defensive-copy time was **1.716 ms** for a full snapshot versus **0.053 ms** for the overlay summary (about **32.5× less copy time**). That ratio applies only to this operation, **not the whole application**.

For 24 cold 1600 × 900 artwork requests, the old synchronous UI path occupied approximately **653.5 ms** in total; the new request/enqueue path occupied approximately **1.5 ms**. All artwork was ready after approximately **651 ms** in the new version. The improvement is responsiveness and scheduling, not a claim of instantaneous image preparation.

## Reproduce on your own machine

Use the same environment as the application:

```powershell
.\.venv\Scripts\python.exe tools\benchmark_performance.py --seconds 12 --json performance-result.json
```

Or, on a headless Linux development machine:

```sh
xvfb-run -a -s '-screen 0 1280x900x24 -nolisten tcp' .venv/bin/python tools/benchmark_performance.py --seconds 12 --json performance-result.json
```

Optional `--scale 1.5`, `--fps 30` or `--screenshot preview.png` arguments help compare workloads. The benchmark opens a demonstration window and uses synthetic data. Native driver/sensor collection and a real game running concurrently should be tested separately on Windows.

## Validation and limits

Regression tests cover deadline pacing, balanced timer ownership, hidden/compact/closed lifecycle, sensor/frame separation, telemetry-copy isolation, caching cadence, long-label/high-DPI layout, bounded/cancelled artwork work, Tk-thread delivery and integration with the launcher. Existing password-reset and data-integrity tests remain in place; full startup now also exercises asynchronous artwork.

The Windows-only suite includes the real multimedia-timer API in addition to DPAPI and BAT checks. Those jobs are configured but have not run in this Linux workspace. GitHub Actions requires committing/pushing the changes; no commit or push was performed here.

### Final validation snapshot — 2026-09-06

- Linux / Python 3.11.16 / Tk 9.0: **131 tests; 125 passed, 6 Windows-only skips**.
- Linux / Python 3.13.14 / Tk 8.6: **131 tests; 125 passed, 6 Windows-only skips**.
- No unexpected Tk callback errors in the final runs.
- Compilation, pyflakes, dependency consistency, whitespace checks and workflow lint passed.
- Existing regression cases were retained. Cryptography, vault storage, record models, runtime requirements and the BAT launcher were verified unchanged from the preceding phase.
- The six unexecuted native checks are Windows DPAPI, the multimedia timer and four BAT cases. Native Windows/game-load validation remains outstanding.

Machine-readable measurements: [performance-results.json](performance-results.json).
