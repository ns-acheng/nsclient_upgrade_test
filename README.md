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

**Upgrade to latest release:**

```bash
python main.py upgrade --target latest --from-version release-92.0.0
```

**Upgrade to latest golden release (no dot release):**

```bash
python main.py upgrade --target golden
```

**Upgrade to latest golden with dot release:**

```bash
python main.py upgrade --target golden --dot
```

**Verify auto-upgrade stays disabled:**

```bash
python main.py upgrade --target disabled --from-version release-92.0.0
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

| Option | Description |
| --- | --- |
| `--target` | **Required.** `latest`, `golden`, or `disabled` |
| `--from-version` | Build version to install before upgrade (required for `latest` and `disabled`) |
| `--dot` | Enable dot release updates for golden upgrade |

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
    installed
- `test/test_util_webui.py` — Tests for WebUI client wrapper:
  - **Connection** — authentication, page object init, missing webapi error,
    auth failure, login timeout
  - **Ensure connected guard** — RuntimeError when not connected
  - **Release versions** — data extraction, sorted version list
  - **Client config** — disable/enable upgrade, golden with/without dot
  - **Device queries** — version lookup
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
