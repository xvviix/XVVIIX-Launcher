# Package architecture — incremental modularization

> Current launch/discovery/icon services and responsive shutdown are described in [launch-scan.md](launch-scan.md). Historical phase notes below retain their original validation context.

> The subsequent [performance/overlay phase](performance.md) adds `services/artwork.py`, `ui/frame_clock.py`, `ui/monitor_overlay.py` and a reproducible benchmark. The architectural/data-compatibility rules below still apply; the validation snapshot at the end records the earlier modularization phase.

This phase separates backend responsibilities from the desktop shell while preserving the existing launcher, encrypted data formats and file locations. It is a refactor, not a new installer or a redesign of the interface.

## Run and upgrade

The existing commands still work:

```powershell
.\.venv\Scripts\python.exe game_launcher.py
```

Or double-click `START_XVVIIX.bat`. `python -m xvviix` is also supported from the project directory.

**Copy/extract the whole project, including the new `xvviix/` folder. Replacing only `game_launcher.py` is no longer sufficient.** The entry point reports an incomplete installation rather than silently starting a different fallback path.

Before upgrading an existing installation, close the launcher and privately back up the entire existing data folder. Keep all generated libraries, vault metadata, recovery files and icon caches. The source ZIP contains application files and synthetic test fixtures, not your personal data.

There are no new third-party runtime dependencies in this phase. Use the existing environment with the pinned `requirements.txt`.

The later [monitor revision](monitor-update.md) retires crash analysis and adds bounded top-five panels. Existing report data remains protected.

The current [library workflow](library-workflow.md) adds keyed card rendering, group actions, activity history and a restricted software catalog.

The [Windows startup integration](windows-startup.md) manages only its named current-user registry values and exposes an in-app off switch.

## Responsibilities

```text
XVVIIX-Launcher/
├── game_launcher.py             # Small, lazy desktop entry point
├── START_XVVIIX.bat              # Windows interpreter/venv selection
├── xvviix/
│   ├── __init__.py               # Safe package import; no app startup
│   ├── __main__.py               # python -m xvviix
│   ├── app.py                    # Composition root and remaining legacy desktop UI
│   ├── paths.py                  # Install, resource and data-directory policy
│   ├── constants.py              # Shared default values, not session state
│   ├── utils.py                  # Formatting, colors and executable paths
│   ├── models.py                 # Persisted-record validation/normalization
│   ├── security/
│   │   └── crypto.py             # scrypt, AES-GCM, key wrapping and Windows DPAPI
│   ├── storage/
│   │   └── vault.py              # VaultStore: data, key state, integrity and backups
│   ├── services/
│   │   ├── audio.py              # Lazy audio backend with explicit callbacks
│   │   ├── hardware_monitor.py   # On-demand telemetry; no Tk UI
│   │   └── artwork.py            # Bounded background image preparation
│   └── ui/
│       ├── vault_dialogs.py      # Login/reset forms with injected storage and theme
│       ├── lifecycle.py          # Root-window callback cleanup
│       ├── top_processes.py      # CPU/RAM top-five panels
│       └── monitor_overlay.py    # Transparent floating panel
├── assets/
├── tests/
│   └── fixtures/                # Public, synthetic pre-refactor vault vectors
└── docs/
```

The telemetry backend remains part of the same launcher and still feeds the built-in Monitor tab/overlay. It does not create another window or event loop. Its network-probe target now lives in `xvviix/services/hardware_monitor.py`.

### VaultStore

`VaultStore` owns one data directory, one encryption-session key, its warning list and its lock. Instances do not share keys or library paths. The application explicitly supplies its existing shared data lock, logging destination, warning sink and runtime-readiness callback.

The storage module imports neither Tk nor `app.py`. It can be tested or used without constructing a desktop window. Its legacy method names and format constants are retained to keep this migration controlled; primitive cryptographic implementations live only in `security/crypto.py`.

The application owns the in-memory library views and decides when to present storage notices. A worker can report an error without calling Tk itself.

### Presentation and services

`VaultDialogs` receives its `VaultStore`, theme, completion callback and optional animation/window-effect callbacks. It does not import the application shell. Password-reset work still runs on a worker, and results still return through a Tk-scheduled queue.

Audio uses explicit diagnostic/error callbacks instead of calling application/UI globals. Importing the audio module does not import pygame. Importing the hardware module does not initialize NVML or start telemetry.

The launcher cancels pending callbacks before destroying its root interpreter. This addresses a stale-callback shutdown error exposed by Tk 9 testing. The cleanup is **not** used when closing a single dialog in a running application.

## Compatibility guarantees checked here

- Writable source installs continue storing data beside the top-level launcher, **not inside `xvviix/`**.
- Protected installs retain the existing per-user fallback policy.
- Frozen resource lookup continues to use `_MEIPASS`; the installation directory remains the executable's directory. No executable was built in this phase.
- Existing v1/v2 metadata, encrypted payload formats, scrypt settings, key-wrap contexts and DPAPI entropy are unchanged.
- Public synthetic ciphertext generated by the pre-refactor implementation is read by the new code; migration/reset tests confirm that the library ciphertext stays unchanged.
- Source entry-point imports and backend-module imports do not start the application, create user files, open Tk, start workers or load the deferred audio/GPU bindings.
- The default password-reset form was compared with the previous screenshot under the same Linux/Tk environment: identical dimensions and pixels. The existing image in `docs/images/password-reset.png` remains valid.

`tests/fixtures/pre_refactor_vaults.json` deliberately contains public, synthetic test credentials/recovery material. It is **not** a user vault and is never loaded by the application. Real vault/recovery files remain excluded from version control and source ZIPs.

Internal Python imports from the former monolithic `game_launcher.py` are not a supported library API. Use the owning package module when writing development tools or new tests.

## Regression coverage

All pre-refactor regression cases are retained; references now target the module/service that owns the behavior. The test harness copies the full package into disposable installation directories and uses isolated package namespaces rather than sharing application state between cases.

New coverage includes headless backend imports, independent vault instances, resource paths, the entry-point exit code, incomplete copies, audio callbacks, telemetry lifecycle, old-format ciphertext and GUI theme/lifecycle behavior.

```sh
python -m pip install -r requirements-dev.txt
python -m compileall -q game_launcher.py xvviix tests
python -m pyflakes game_launcher.py xvviix tests
python -m unittest discover -s tests -v
```

For headless Linux UI tests:

```sh
xvfb-run -a -s '-screen 0 1280x900x24 -nolisten tcp' python -m unittest discover -s tests -v
```

The GitHub workflow now compiles/lints the **entire package**, not just the entry point. It still needs to be pushed to GitHub to run there. Native Windows execution, including DPAPI and the BAT tests, must be checked on Windows; Linux results are not a substitute.

## Deliberately not rewritten yet

This is incremental modularization, not a claim that all global state has disappeared. `app.py` still contains the legacy game-library UI, scanner/process orchestration, system-report collection and most visual effects. Those can be separated behind smaller controllers/services when the corresponding features are changed, using the regression suite as a baseline.

No tagging, launch profiles, one-click primary-data recovery, new telemetry feature, or `.exe` installer is included in this phase. The next product improvements remain separate tasks rather than being mixed into this refactor.

## Validation snapshot — 2026-09-06

| Check | Result |
|---|---|
| Linux / Python 3.11.16 / Tk 9.0 | 100 tests discovered; 95 passed, 5 Windows-only tests skipped |
| Linux / Python 3.13.14 / Tk 8.6 | 100 tests discovered; 95 passed, 5 Windows-only tests skipped |
| Pre-refactor synthetic v1/v2 vault files | Opened correctly; library ciphertext preserved |
| Full startup, reset with a bad backup, and shutdown | Passed; no unexpected stale-callback errors in the final test runs |
| Default reset-dialog screenshot comparison | 560 × 475 pixels; zero changed pixels under the same Linux/Tk setup |
| Compilation and pyflakes across entry point, package and tests | Passed on both Python versions |
| Dependency consistency and GitHub workflow lint | Passed locally |
| Native Windows and actual GitHub Actions execution | Not run in this Linux workspace; Windows tests remain configured in CI |

All original regression cases were retained. The test references were changed to their owning services/modules, not replaced with weaker expectations. No commit or push was performed in this phase.
