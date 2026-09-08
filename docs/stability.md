# Stability phase — vault integrity, Windows startup and CI

> These notes describe the preceding stability phase. The current source is packaged under `xvviix/`; see [architecture and upgrade instructions](architecture.md). The data-integrity rules below still apply.

This phase preserves the current interface and code-free Forgot Password flow. It does not perform the larger module refactor, add an installer, or add automatic one-click restoration of damaged primary data.

## Upgrade an existing installation

1. Close XVVIIX and make a private copy of the entire existing installation/data folder, including the matching vault metadata and local recovery file.
2. Replace the application source, requirements, assets and documentation with the new version. Do not delete your generated JSON files, recovery material or backups. Source archives distributed from this workspace do not contain those personal files.
3. Install the pinned requirements into the existing **Windows** virtual environment:

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

   If there is no Windows environment yet, create one first:

   ```powershell
   py -3 -m venv .venv
   ```

4. Double-click `START_XVVIIX.bat`. It now selects `.venv\Scripts\python.exe` before trying `py -3` or `python` from PATH. A Linux virtual environment is not portable to Windows and is not included in source ZIPs.

The launcher keeps the application's exit code, reports missing source/Python clearly, and avoids delayed expansion so that an exclamation mark in the project path is not interpreted as a variable. Line-ending rules preserve CRLF for `.bat` files.

## Recovery rules

| Situation | Behavior |
|---|---|
| Current data is valid, `.bak` is damaged | Unlock and local password reset continue with a warning. The bad backup is neither trusted nor modified by those operations. |
| A historical `.corrupt-*` archive is damaged | It is kept and reported, but does not block otherwise valid current data. |
| A normal save would rotate over a damaged `.bak` | First preserve its exact bytes in a private, uniquely named `.corrupt-backup-*` file; only then write a healthy backup atomically and commit the new primary. Identical archived bytes are reused rather than copied repeatedly. |
| Preserving the damaged backup fails | Refuse the save. Do not overwrite either original file. |
| Current primary fails authentication or JSON-array validation | Stop loading/resetting. Never substitute an empty library or overwrite the good backup with the bad primary. |
| Current primary is missing but its `.bak` exists | Stop and report it. Do not create an empty file over a potentially recoverable category. |
| A category file is missing with no `.bak` | Normal unlock retains legacy initialization behavior: create an empty encrypted category **with an explicit notice**. Local password reset does not initialize missing primaries. |
| A valid legacy plaintext category/backup exists | Normal unlock can migrate it to encryption. Every primary is validated before any primary migration is written. Malformed existing primary data prevents initial vault creation or migration. |
| A load/write fails after startup | Keep the original data and report the failure; save notifications are queued onto the Tk thread rather than shown from worker threads. |

Warnings are deduplicated during a session. Current records are authenticated and their top-level JSON type is validated; ignoring a bad *backup* does not bypass checks on the *primary*.

Preserved artifacts are private local recovery evidence, not files to publish. Exact-byte preservation also means that a previously unencrypted/malformed legacy artifact is not magically encrypted by being archived. Keep the full data folder private and never attach it to a public issue.

## Restoring a damaged primary — explicit user action

When the launcher reports that a **valid encrypted `.bak`** is available, it has checked that backup with the current data key. Restoration is intentionally not automatic: the backup may be older than the last state you remember.

1. Close the launcher and copy the entire data folder somewhere private.
2. Keep any damaged original under a unique archive name, for example `games.json.corrupt-manual-20260906`. Do not overwrite another archive.
3. **Copy**, rather than move, the matching `games.json.bak` to `games.json`.
4. Reopen the launcher and sign in. Check the restored entries and playtime before making more changes.

Use the equivalent filenames for apps, discovery, reports or activity. Keep `xvviix_vault.json`, its backup and any `xvviix_local_recovery.json` with their matching data. Do not restore a data file from a different vault. Without a verified backup, the application leaves the damaged data alone rather than guessing or deleting it.

## Repeatable dependencies

- `requirements.txt` pins the runtime packages and their current transitive dependencies for CPython.
- `requirements-dev.txt` includes the runtime set and the pinned `pyflakes` version. Tests use standard-library `unittest`.
- These are **version pins**, not a hash-verified lock for every platform. Linux and Windows necessarily use different wheels.
- The CI matrix targets Python **3.11 and 3.13**, on Linux and Windows. Updates should be made deliberately and validated across the matrix; do not blindly upgrade a working installation.
- No new runtime package was added for the vault changes or the BAT fix.

## Automated checks

`.github/workflows/checks.yml` is configured for pushes to `main`, pull requests and manual dispatch. It uses read-only repository permissions, commit-pinned GitHub actions, and does not retain checkout credentials.

Each Linux/Windows and Python 3.11/3.13 combination installs the pinned dependencies, checks them, compiles the source, runs `pyflakes`, and runs regression tests. Linux uses Xvfb for Tk dialogs. Windows additionally runs real DPAPI and `cmd.exe`/BAT checks, including virtual-environment priority and exit-code propagation.

A workflow file in a local workspace or ZIP does **not** mean GitHub Actions has run: it must be committed/pushed to GitHub with Actions enabled. Local Linux results and cross-platform wheel resolution are not substitutes for executing the native Windows jobs.

Run checks locally from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m compileall -q game_launcher.py xvviix tests
.\.venv\Scripts\python.exe -m pyflakes game_launcher.py xvviix tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

On Linux:

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
xvfb-run -a -s '-screen 0 1280x900x24 -nolisten tcp' .venv/bin/python -m unittest discover -s tests -v
```

Tests create disposable data and do not touch the real user's vault. Windows-only tests skip elsewhere; POSIX permission checks skip on Windows. GPU drivers, actual sound output and every Windows scanner integration are outside this regression suite.

## Validation snapshot — 2026-09-06

| Check performed in this workspace | Result |
|---|---|
| Linux, Python 3.11.16 / Tk 9.0 | 76 tests discovered; 71 passed, 5 Windows-only tests skipped |
| Linux, Python 3.13.14 / Tk 8.6 | 76 tests discovered; 71 passed, 5 Windows-only tests skipped |
| Full launcher startup after local reset with a damaged backup | Passed as a regression test: existing data opened, warning delivered, normal shutdown, original data/backup bytes preserved |
| Compile and pyflakes, both local Python versions | Passed |
| Installed dependency consistency | Passed on the primary development environment |
| Windows x64 wheel resolution for Python 3.11 and 3.13 | Passed; download/resolution only, not native execution |
| GitHub workflow validation | Passed with actionlint 1.7.12 |
| Real Windows DPAPI and BAT execution for this phase | Not executed in this Linux workspace; covered by Windows-only tests configured in CI |

No commit or push was performed during this workspace phase. The new GitHub workflow has been validated locally, not executed on GitHub. The full-launcher regression uses plain Tk and muted audio; it is not a test of DND, physical audio output or GPU drivers.
