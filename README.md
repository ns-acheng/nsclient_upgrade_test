# Netskope Client Auto-Upgrade Tool

CLI tool for testing Netskope Client auto-upgrade scenarios. It orchestrates
upgrade workflows by coordinating between a tenant's WebUI API and the local
Netskope Client installation.

## Requirements

- Python 3.10+
- Access to a Netskope tenant with admin credentials
- Netskope Client installed on the test machine

## Installation

```bash
pip install -r requirements.txt
```

## Usage

### First-Time Setup

Save your tenant hostname and admin username to `data/config.json`.
Password is **never** saved — you will be prompted each run.

```bash
python main.py setup
```

### List Available Versions

Query the tenant for all available client release versions:

```bash
python main.py versions
```

### Check Local Client Status

Show the currently installed client version and status:

```bash
python main.py status
```

### Run Upgrade Scenarios

**Upgrade to latest release:**

```bash
python main.py upgrade --target latest --from-version release-92.0.0
```

**Upgrade to golden release (latest golden, no dot release):**

```bash
python main.py upgrade --target golden
```

**Upgrade to golden with dot release:**

```bash
python main.py upgrade --target golden --golden-index -1 --dot
```

**Upgrade to N-1 golden:**

```bash
python main.py upgrade --target golden --golden-index -2
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
| `--golden-index` | Golden version index: `-1` = latest, `-2` = N-1, `-3` = N-2 (default: `-1`) |
| `--dot` | Enable dot release updates for golden upgrade |

## Unit Tests

Tests are in the `test/` directory and use **pytest** with all I/O mocked. No
network access, tenant connection, or admin privileges are needed to run them.

### Running Tests

```bash
python -m pytest test/ -v
```

### Test Structure

- `test/conftest.py` — Mocks external packages (`nsclient`, `webapi`) so tests
  run without those dependencies installed.
- `test/test_upgrade_runner.py` — Tests for the upgrade orchestration logic,
  covering:
  - **Upgrade to latest** — success, timeout, cleanup on success, cleanup on
    exception
  - **Upgrade to golden** — latest golden (with/without dot), N-1 golden,
    auto-pick of from-version
  - **Upgrade disabled** — verifies version stays unchanged, detects unexpected
    upgrades
  - **WebUI verification** — version mismatch handling, API error resilience
  - **Prepare client** — uninstall-before-install flow, skip-uninstall when not
    installed

### Adding New Tests

Create test files following the pattern `test/test_<module_name>.py`. Mock all
external I/O (file system, network, OS calls) and use the shared fixtures in
`conftest.py`.
