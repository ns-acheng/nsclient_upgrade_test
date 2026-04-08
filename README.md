# Netskope Client Auto-Upgrade Tool

CLI tool for testing Netskope Client auto-upgrade scenarios. It orchestrates
upgrade workflows by coordinating between a tenant's WebUI API and the local
Netskope Client installation.

## Requirements

- Python 3.10+
- Access to a Netskope tenant with admin credentials
- Netskope Client installed on the test machine
- `pylark-webapi-lib` — Netskope internal WebUI API library (not included)
- `nsclient` — Netskope Client library (required for `status` and `upgrade`)

## Installation

```bash
pip install -r requirements.txt
```

### pylark-webapi-lib (required)

This tool depends on `pylark-webapi-lib` for tenant WebUI API calls. It must be
installed separately. Clone the repo and install it:

```bash
git clone <pylark-webapi-lib repo URL>
pip install -e /path/to/pylark-webapi-lib
```

If this package is not installed, the tool will show a clear error when you try
to connect to a tenant. Unit tests do **not** require it — all webapi calls are
mocked.

## Usage

### First-Time Setup

Save your tenant hostname, admin username, and encrypted password to
`data/`. The password is encrypted with a local key and stored in
`data/.password.enc` — both the key and password file are git-ignored and
never committed.

```bash
python main.py setup
```

On subsequent runs the saved password is used automatically. If the password
is wrong, you will be prompted up to 3 times before the tool aborts.

### List Available Versions

Query the tenant for all available client release versions:

```bash
python main.py versions
```

### Check Local Client Status

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

The tool picks the correct file based on platform and the `--64bit` flag:

| Platform | Flag | Expected file |
| --- | --- | --- |
| Windows | *(default)* | `STAgent.msi` |
| Windows | `--64bit` | `STAgent64.msi` |
| macOS | | `STAgent.pkg` |
| Linux | | `STAgent.run` |

If the exact filename is found, it is used directly. If the directory
contains a single file with a different name, it is automatically renamed.
If no matching file is found, the tool falls back to downloading the build
from the build server using `--from-version`.

#### Send email invite (optional)

Use `--email` to send an enrollment email invite to a user before the
upgrade starts. The tool will prompt you for the download link from the
email, then rename the base installer to the tenant-specific name before
installing:

```bash
python main.py upgrade --target latest --email user@example.com
python main.py upgrade --target golden-dot --email user@example.com
```

#### Upgrade to latest release

```bash
python main.py upgrade --target latest

# Use 64-bit installer
python main.py upgrade --target latest --64bit
```

#### Upgrade to latest golden release (base version only)

```bash
python main.py upgrade --target golden
```

#### Upgrade to latest golden with dot release

```bash
python main.py upgrade --target golden-dot
```

#### Verify auto-upgrade stays disabled

A separate command that installs the base version with auto-upgrade disabled,
waits, and verifies the client does **not** upgrade (negative test):

```bash
python main.py disable-upgrade

# Use 64-bit installer
python main.py disable-upgrade --64bit
```

### Global Options

| Option | Description |
| --- | --- |
| `--config PATH` | Path to config JSON (default: `data/config.json`) |
| `--tenant HOST` | Tenant hostname (overrides config) |
| `--username USER` | Admin username (overrides config) |
| `--password PASS` | Admin password (overrides config) |
| `-v, --verbose` | Enable debug logging |

### Upgrade Options

These options apply to both `upgrade` and `disable-upgrade` commands:

| Option | Description |
| --- | --- |
| `--target` | **Required** (upgrade only). `latest`, `golden`, or `golden-dot` |
| `--from-version` | Build version for download fallback when no local installer is available (e.g. `123.0.0`) |
| `--64bit` | Use 64-bit client installer (Windows only) |
| `--email` | Send enrollment email invite before upgrade (optional) |

## Unit Tests

Tests are in the `test/` directory and use **pytest** with all I/O mocked. No
network access, tenant connection, or admin privileges are needed to run them.
Neither `pylark-webapi-lib` nor `nsclient` need to be installed.

### Running Tests

```bash
python -m pytest test/ -v
```

### Test Structure

- `test/conftest.py` — Mocks external packages (`nsclient`, `webapi`) so tests
  run without those dependencies installed.
- `test/test_main.py` — Tests for CLI command output and login flow:
  - **cmd_versions** — version display, golden build display, timestamp
    filtering, connect failure handling
  - **connect_with_retry** — success on 1st/2nd/3rd attempt, failure after 3
    attempts, non-auth error propagation, timeout handling, password clearing
  - **nsclient check** — status and upgrade abort cleanly when nsclient is
    missing
- `test/test_upgrade_runner.py` — Tests for the upgrade orchestration logic:
  - **Upgrade to latest** — success, timeout, cleanup on success, cleanup on
    exception
  - **Upgrade to golden** — latest golden (with/without dot), auto-pick of
    from-version
  - **Upgrade disabled** — verifies version stays unchanged, detects unexpected
    upgrades
  - **WebUI verification** — version mismatch handling, API error resilience
  - **Prepare client** — uninstall-before-install flow, skip-uninstall when not
    installed, local installer exact match, 64-bit selection, single-file rename,
    fallback to download, ambiguous multi-file fallback, missing installer error
- `test/test_util_webui.py` — Tests for WebUI client wrapper:
  - **Connection** — authentication, page object init, missing webapi error,
    auth failure, login timeout
  - **Ensure connected guard** — RuntimeError when not connected
  - **Release versions** — data extraction, sorted version list
  - **Client config** — disable/enable upgrade, golden with/without dot
  - **Device queries** — version lookup
  - **Email invite** — send invite, guard when not connected
- `test/test_util_secret.py` — Tests for encrypted password storage:
  - **Round-trip** — save/load, overwrite, empty, unicode
  - **Missing data** — no files, missing key, missing password, corrupt data,
    wrong key
  - **Clear** — file removal, no-op when absent
  - **Key generation** — creation and reuse

### Adding New Tests

Create test files following the pattern `test/test_<module_name>.py`. Mock all
external I/O (file system, network, OS calls) and use the shared fixtures in
`conftest.py`.
