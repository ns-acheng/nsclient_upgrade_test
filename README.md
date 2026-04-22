# Netskope Client Auto-Upgrade Tool

CLI tool for testing Netskope Client auto-upgrade scenarios. It orchestrates
upgrade workflows by coordinating between a tenant's WebUI API and the local
Netskope Client installation.

Primary workflow: run upgrade scenarios with `python main.py upgrade --target ...`.

## Requirements

- Python 3.10+
- Access to a Netskope tenant with admin credentials
- Netskope Client installed on the test machine
- `pylark-webapi-lib` — Netskope internal WebUI API library (not included)
- `nsclient` — Netskope Client library (required for `status` and `upgrade`)
- `selenium` — For Gmail email auto-extraction (auto-installed via requirements.txt)

## Installation

```bash
pip install -r requirements.txt
```

### pylark-webapi-lib (required)

This tool depends on `pylark-webapi-lib` for tenant WebUI API calls. It must be
installed separately. Clone the repo and install it:

```bash
git clone [<pylark-webapi-lib repo URL>](https://github.com/netskope-qe/pylark-webapi-lib)
pip install -e /path/to/pylark-webapi-lib
```

If this package is not installed, the tool will show a clear error when you try
to connect to a tenant. Unit tests do **not** require it — all webapi calls are
mocked.

## Usage

### Primary Workflow (Upgrade)

Use `upgrade` for the main test flow:

```bash
# Latest release
python main.py upgrade --target latest

# Latest golden release (base build)
python main.py upgrade --target golden

# Specific golden version with dot release
python main.py upgrade --target golden --golden-version 132 --dot

# Local MSI upgrade (from data/upgrade_version/)
python main.py upgrade --target local --email user@example.com
```

All upgrade scenarios and options are documented below in **Run Upgrade Scenarios**.

### Other Commands (Lower Priority)

#### First-Time Setup

Save your tenant hostname, admin username, and encrypted password to
`data/`. The password is encrypted with a local key and stored in
`data/.password.enc` — both the key and password file are git-ignored and
never committed.

```bash
python main.py setup
```

On subsequent runs the saved password is used automatically. If the password
is wrong, you will be prompted up to 3 times before the tool aborts.

#### List Available Versions

Query the tenant for all available client release versions:

```bash
python main.py versions
```

#### Check Local Client Status

Show the currently installed client version and status (requires `nsclient`):

```bash
python main.py status
```

### Run Upgrade Scenarios

All upgrade commands require `nsclient` to be installed. The tool checks for
it up front and aborts with a clear message if missing.

#### Base version installer

Before running an upgrade, place the old version installer(s) in the
`data/base_version/` directory. The tool supports both 32-bit and 64-bit
Windows installers:

```
data/base_version/
  STAgent.msi      <-- 32-bit installer
  STAgent64.msi    <-- 64-bit installer
```

The tool picks the correct file based on platform and the `--source-64bit` flag:

| Platform | Flag | Expected file |
| --- | --- | --- |
| Windows | *(default)* | `STAgent.msi` |
| Windows | `--source-64bit` | `STAgent64.msi` |


If the exact filename is found, it is used directly. If the directory
contains a single file with a different name, it is automatically renamed.
If no matching file is found, the tool falls back to downloading the build
from the build server using `--from-version`.

#### Send email invite (optional)

Use `--email` to send an enrollment email invite to a user before the
upgrade starts. The tool automatically extracts the download link from
Gmail using Selenium. If auto-extraction fails, it falls back to a
manual paste prompt:

```bash
python main.py upgrade --target latest --email user@example.com
python main.py upgrade --target golden --golden-version 132 --dot --email user@example.com
```

Chrome handling:
- If Chrome is already running on debug port 9222, Selenium attaches to it.
- If not, the tool auto-launches Chrome with `--remote-debugging-port=9222`
  and a local profile (`local_profile/`), then navigates to Gmail.
- On first use, sign into Gmail manually once — subsequent runs reuse the
  session via the local profile.

#### Upgrade to latest release

```bash
python main.py upgrade --target latest

# 64-bit source and target
python main.py upgrade --target latest --source-64bit --target-64bit

# Cross-architecture: 32-bit base upgrading to 64-bit target
python main.py upgrade --target latest --target-64bit
```

#### Upgrade to golden release (`--target golden`)

Upgrades to any golden channel version. By default the latest golden version
on the tenant is used. Use `--golden-version` to target a specific version,
and `--dot` to upgrade to the latest dot release within that golden version.

```bash
# Latest golden — base build only
python main.py upgrade --target golden

# Latest golden — with dot release
python main.py upgrade --target golden --dot

# Specific golden version 132 — base build only
python main.py upgrade --target golden --golden-version 132

# Specific golden version 132 — with dot release
python main.py upgrade --target golden --golden-version 132 --dot

# Full version string also accepted
python main.py upgrade --target golden --golden-version 132.0.0 --dot

# 64-bit upgrade to golden 135
python main.py upgrade --target golden --golden-version 135 --target-64bit
```

`--golden-version` accepts either short form (`132`) or full form (`132.0.0`).
If the specified version is not available on the tenant, the tool fails
immediately and lists all available golden versions.

#### Upgrade from local MSI (`--target local`)

Installs the base client, then immediately upgrades it using local MSI files
from `data/upgrade_version/`. No tenant WebUI upgrade config is needed — the
upgrade is triggered directly by `msiexec`. The timing monitor starts at the
same time as the MSI install so all lifecycle events are captured.

Place the upgrade MSI(s) in `data/upgrade_version/` before running:

```
data/upgrade_version/
  stagent.msi      <-- 32-bit upgrade MSI
  stagent64.msi    <-- 64-bit upgrade MSI
```

The correct file is chosen automatically based on `--target-64bit`.

```bash
# 32-bit upgrade (uses stagent.msi)
python main.py upgrade --target local

# 64-bit upgrade (uses stagent64.msi)
python main.py upgrade --target local --target-64bit

# With email invite for base install token
python main.py upgrade --target local --email user@example.com

# With reboot at timing 6
python main.py upgrade --target local --target-64bit --reboottime 6

# With simulation pre-actions (see below)
python main.py upgrade --target local --simulate
```

**Expected version** is read from the MSI's Subject field (Windows Installer
summary info) so no `--from-version` is needed for the upgrade target.

When `--simulate` is set, the tool performs these actions immediately before
executing the upgrade MSI:

1. Set registry DWORD `HKLM\SOFTWARE\Netskope\UpgradeInProgress = 1`
2. Update `C:\ProgramData\netskope\stagent\nsconfig.json` (root `cache` node):
   - `cache.lastClientUpdated = "1"`
   - `cache.newClientVer = "137.0.0.2222"`

#### Upgrade with timing monitor

Monitor 13 upgrade lifecycle events in a background thread while the
upgrade runs. Optionally trigger a reboot at a specific timing:

```bash
# Monitor only (no reboot) -- timing report prints after upgrade completes
python main.py upgrade --target latest --source-64bit --target-64bit --reboottime 5 --rebootdelay 0

# Reboot when timing 6 fires (stAgentUI.exe is gone), delay 5s
python main.py upgrade --target latest --source-64bit --reboottime 6 --rebootdelay 5
```

The 13 monitored timings:

| # | Event |
|---|-------|
| 1 | nsconfig.json clientUpdate.allowAutoUpdate = true |
| 2 | STAgent.msi downloaded (>25 MB) |
| 3 | stAgentSvcMon.exe -monitor starts |
| 4 | MSIExec process start with argument /i or /I |
| 5 | nsInstallation.log created/updated |
| 6 | stAgentUI.exe is gone |
| 7 | stAgentSvc service stop_pending |
| 8 | stAgentSvc stopped / stop_pending / gone |
| 9 | stadrv service stopped/gone |
| 10 | stAgentSvc service removed from SCM |
| 11 | New stAgentSvc.exe in target install dir |
| 12 | stAgentSvc.exe running with new PID |
| 13 | stAgentSvcMon.exe stopped & upgraded |

> **Note:** Timing 3 never fires in watchdog mode — the tool detects this
> and automatically skips `--reboottime 3` tests with a PASS result.

When `--reboottime` triggers a reboot, the tool saves monitor state to
`data/monitor_state.json` and creates a scheduled task to run
`python main.py continue` immediately after user logon. The `continue` command
resumes monitoring, prints the final timing report, and cleans up.

#### Upgrade with extra action
These actions can be take during reboot:

| # | Event |
|---|-------|
| 2 | kill stAgentMon |
| 3 | kill stAgentMon and MsiExec |
| 4 | kill stAgentMon, stAgent and MsiExec |


#### Resume after reboot

Automatically called by the scheduled task after a monitor-triggered reboot.
Can also be run manually:

```bash
python main.py continue --timeout 600
```

#### Verify auto-upgrade stays disabled

A separate command that installs the base version with auto-upgrade disabled,
waits, and verifies the client does **not** upgrade (negative test):

```bash
python main.py disable-upgrade

# Use 64-bit installer
python main.py disable-upgrade --source-64bit
```

#### Reboot-interrupt test

Two-phase test that enables upgrade, reboots during the upgrade process,
and verifies the client recovers correctly:

```bash
# Phase 1: Enable upgrade and schedule reboot
python main.py reboot-setup --target latest --reboot-timing mid --source-64bit --target-64bit

# Phase 2: Runs automatically after logon (no delay), or manually:
python main.py reboot-verify
```

---

## Batch Runner

`batch.py` runs a sequence of upgrade tests unattended, records results to
JSON, and generates an HTML report. It supports resuming after interruption
and survives machine reboots triggered by `--reboottime` tests.

### How it works

Each test is `base_args` + `extra_args`, run as a `python main.py ...`
subprocess. Results are written to `log/batch_record.json` after every test
so progress is never lost. An HTML report is regenerated after each test at
`log/batch_report.html`.

Use `--local` to switch to the local profile files:

- `data/batch_local.json`
- `log/batch_record_local.json`
- `log/batch_report_local.html`

For reboot tests, batch.py registers a Windows scheduled task
(`NsClientBatchContinue`, ONLOGON) **before** launching the subprocess. After
the machine reboots and the user auto-logs in, the task calls
`batch.py --continue`, which runs `main.py continue` to finish monitoring, then
resumes the remaining tests.

### Define your tests — `data/batch.json`

```json
{
    "base_args": "upgrade --target latest --email acheng@netskope.com",
    "tests": [
        {"id": "32to32",        "extra_args": ""},
        {"id": "32to64",        "extra_args": "--target-64bit"},
        {"id": "64to64",        "extra_args": "--target-64bit --source-64bit"},
        {"id": "32to64_rb1",    "extra_args": "--target-64bit --reboottime 1"},
        {"id": "32to64_rb1_a2", "extra_args": "--target-64bit --reboottime 1 --action 2"}
    ]
}
```

- `base_args` — the subcommand and flags shared by all tests (must start with `upgrade` or `disable-upgrade`)
- `tests` — list of test objects with a unique `id` and `extra_args` appended to `base_args`
- Plain strings are also accepted in `tests`; IDs are auto-generated as `test_00`, `test_01`, …

A pre-populated template covering the common 32/64-bit and reboot-timing
combinations is included in `data/batch.json`.

### Commands

| Command | Description |
|---------|-------------|
| `python batch.py` | Fresh run — prompts if a record already exists (see below) |
| `python batch.py --local` | Same flow, but uses local profile files |
| `python batch.py --resume` | Resume from existing record, skip completed tests |
| `python batch.py --retry-failed` | Reset all failed tests to pending and re-run |
| `python batch.py --retry ID [ID ...]` | Reset specific test(s) by ID and re-run |
| `python batch.py --merge record1.json record2.json` | Merge external records into current record and regenerate report |
| `python batch.py --merge --local record1.json record2.json` | Merge into local profile record and regenerate local report |
| `python batch.py --continue` | Resume after reboot (called automatically by scheduled task) |
| `python batch.py --report` | Re-generate HTML report without running any tests |
| `python batch.py --fresh` | Silently delete the existing record and start over |

**Options:**

| Option | Description |
|--------|-------------|
| `--local` | Use local profile files (`data/batch_local.json`, `log/batch_record_local.json`, `log/batch_report_local.html`) |
| `-v` | Verbose logging |

### Merge behavior (`--merge`)

`--merge` loads one or more existing batch record JSON files and merges test
results into the currently selected record profile:

- default profile: `log/batch_record.json`
- local profile (`--local`): `log/batch_record_local.json`

For each matching test ID:

- if target test has no result yet and source has data, source is copied
- if both have data, the newer result is kept (by timestamp)
  - compares `finished_at` first, then `started_at`

After merge, the corresponding HTML report is regenerated automatically.

### Re-running failed tests

To re-run all failed tests without touching passed ones:

```bash
python batch.py --retry-failed
```

To re-run specific tests by ID:

```bash
python batch.py --retry 32to64_rb1 64to64_rb1_a2
```

Both commands load the existing record, reset the target tests to `pending`
(clearing their previous results), and continue the batch from there.  Tests
that are already `pass` or still `pending` are left untouched unless
explicitly named in `--retry`.

If a `--retry` ID is not found in the record, a warning is printed and the
rest proceed normally.

### Crash / interruption recovery

`batch_record.json` is saved after every test, so progress is never lost.
To resume after a crash, power failure, or manual kill:

```bash
python batch.py --resume
```

This skips all `pass`/`fail` tests and re-runs from the first `pending` or
`running` test (a test stuck in `running` due to a crash is re-started from
scratch).

### Existing record prompt

When `batch.py` is run without `--resume` or `--fresh` and
`batch_record.json` already exists, the tool prompts:

```
  Existing batch found: 20260411_090000 (12/160 complete)
  [o] Overwrite  [b] Backup and start fresh  (default: o)
  Choice:
```

- **o** (default) — overwrite the existing record and start fresh.
- **b** — rename the existing record to `batch_record_<batch_id>.json`
  (preserving it for reference), then start fresh.

To skip the prompt entirely, use `--fresh`.

### Batch record — `log/batch_record.json`

Persisted after every test. Each entry tracks:

```
id, extra_args, status (pending/running/pass/fail),
version_before, version_after, expected_version,
elapsed_seconds, message, log_dir, started_at, finished_at
```

`started_at` and `finished_at` are ISO-8601 timestamps written by `main.py`
into the result file and copied into the record — they reflect the actual
wall-clock times of the test run, not when the batch runner processed them.

### HTML report — `log/batch_report.html`

Self-contained, no external dependencies. Open in any browser.

- Color-coded status badges: green = PASS, red = FAIL, yellow = RUNNING, gray = PENDING
- Summary row with total pass/fail/pending counts
- Per-test table: ID, args, status, version before/after, start time, elapsed, log link, message
- Re-generated live after each test completes and on demand with `--report`

### Manual run → batch record

Running `main.py upgrade ...` outside of `batch.py` still updates the batch
record automatically on success.  After a successful manual run, the tool:

1. Loads `log/batch_record.json` (creates it from `data/batch.json` if it
   does not exist yet).
2. Normalises the command-line arguments and finds the matching test in the
   record (order-insensitive; meta-flags like `-v`, `--config`, `--password`
   are ignored).
3. Marks the matching test as `pass` and updates `batch_report.html`.
4. If the same test is re-run and passes again, the record is overwritten
   with the latest result.

Failed manual runs are **not** recorded — only successes update the batch.

### Argument logging

Every `main.py` invocation logs its full argument list to both console and
the run's log file at INFO level:

```
INFO  main.py upgrade --target latest --email acheng@netskope.com --target-64bit
```

This makes it easy to reproduce any run from the log.

### Multi-email Chrome profiles

When tests use different `--email` addresses, each email is automatically
assigned its own Chrome user-data directory so Gmail sessions don't conflict:

- First email seen → `local_profile/`
- Second email → `local_profile2/`
- Third email → `local_profile3/`, and so on

The mapping is persisted in `data/config.json` under `client.email_profiles`
so the same email always gets the same profile across runs.

### Global Options

| Option | Description |
| --- | --- |
| `--config PATH` | Path to config JSON (default: `data/config.json`) |
| `--tenant HOST` | Tenant hostname (overrides config) |
| `--username USER` | Admin username (overrides config) |
| `--password PASS` | Admin password (overrides config) |
| `-v, --verbose` | Enable debug logging |

### Upgrade Options

| Option | Description |
| --- | --- |
| `--target` | **Required** (upgrade only). `latest`, `golden`, or `local` |
| `--golden-version N` | Golden version to target, e.g. `132` or `132.0.0`. Defaults to latest golden. Only used with `--target golden` |
| `--dot` | Upgrade to the latest dot release within the golden version. Only used with `--target golden` |
| `--from-version` | Build version for download fallback (e.g. `123.0.0`) |
| `--source-64bit` | Source (base) install is 64-bit |
| `--target-64bit` | Upgrade target is 64-bit |
| `--email` | Send enrollment email invite before upgrade |
| `--reboottime N` | Timing number (1-13) that triggers a reboot during upgrade |
| `--rebootdelay N` | Seconds to wait after timing fires before rebooting (default: 5) |
| `--simulate` | Local-target only: set `HKLM\SOFTWARE\Netskope\UpgradeInProgress` DWORD=1 and update `nsconfig.json` cache before local MSI install |


## Unit Tests

Tests are in the `test/` directory and use **pytest** with all I/O mocked. No
network access, tenant connection, or admin privileges are needed to run them.
Neither `pylark-webapi-lib` nor `nsclient` need to be installed.

### Running Tests

```bash
python -m pytest test/ -v
```


### Adding New Tests

Create test files following the pattern `test/test_<module_name>.py`. Mock all
external I/O (file system, network, OS calls) and use the shared fixtures in
`conftest.py`.
