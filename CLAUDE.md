# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

New tool project under development. Part of the Netskope Client tooling ecosystem.

## Python Requirements

- **Minimum Version**: Python 3.10+
- **No virtual environment** — install/run directly in the current global Python environment
- **Install dependencies**: `pip install -r requirements.txt`

## Code Style & Rules

### General

- **Line length**: Limit each line to 110 characters
- **Line endings**: Use Windows CRLF (`\r\n`) line breaks for all files
- **Naming**:
  - snake_case for files, functions, and variables
  - PascalCase for class names
  - UPPER_SNAKE_CASE for constants
  - Prefix interfaces with `I` (e.g., `IPowerManager`)
  - Prefix utility files with `util_` (e.g., `util_log.py`)
- **Type hints**: Use type hints for all function parameters and return types

### Import Organization

Organize imports in three groups with blank lines between:

```python
# 1. Standard library
import sys
import os

# 2. Third-party packages
import requests

# 3. Local modules
from util_config import ToolConfig
```

### Documentation

- Add docstrings for public classes and non-trivial methods
- Use comments only where logic isn't self-evident
- Keep comments concise and relevant

## Architecture Principles

- **CLI entry point**: Use `argparse` for command-line argument parsing
- **Utility modules**: Separate concerns into `util_<name>.py` files
- **Configuration**: JSON config files stored in `data/`
- **Caching**: Cache API/file results locally in `cache/` folders
- **Cross-platform**: If needed, use ABC interfaces in `interfaces/` + factory pattern with platform implementations in `platforms/<platform>/`

### File and Directory Organization

```
data/                # Configuration and data files (JSON)
cache/               # Cached results and downloads
interfaces/          # Abstract interfaces (ABC) if cross-platform
platforms/           # Platform-specific implementations if cross-platform
  windows/
  macos/
  linux/
tool/                # Helper scripts
test/                # Unit tests
log/                 # Log output
```

## Logging

- Use Python's `logging` module exclusively
- **Levels**: INFO for normal ops, WARNING for non-blocking issues, ERROR for failures
- **Security**: Never log passwords, tokens, credentials, or sensitive data
- Set verbose third-party loggers to WARNING level

## Testing

- **Framework**: pytest
- **Test files**: `test/test_<module_name>.py`
- **Mock all I/O**: file system, network, and OS calls
- **No admin privileges** required to run tests
- Run: `python -m pytest test/ -v`

## Error Handling

- Wrap I/O and external calls in try-except blocks
- Use `logger.exception()` for full stack traces
- Clean up resources in `finally` blocks

## Security

- Never log or commit passwords, tokens, or credentials
- Validate all user inputs and configuration values
- Avoid shell command injection

## NSClient Knowledge

### nsconfig.json

The local Netskope Client stores its configuration at
`C:\ProgramData\netskope\stagent\nsconfig.json`. Key fields used by this tool:

- **`nsgw.host`** — Gateway hostname. Strip the `gateway-` prefix to get the
  tenant hostname (e.g. `gateway-tenant.goskope.com` → `tenant.goskope.com`).
- **`clientConfig.configurationName`** — The client configuration name assigned
  to this device on the tenant (e.g. `"acheng config"`). This is **not** the
  default config. All WebUI API calls (`update_client_config`,
  `disable_auto_upgrade`, `enable_upgrade_*`, `set_upgrade_schedule`) must pass
  the correct `config_name` as `search_config` so changes apply to the right
  configuration — otherwise they silently save to the default tenant config.
- **`clientConfig.nsclient_watchdog_monitor`** — Watchdog mode flag. **Always
  nested under `clientConfig`**, never at the top level. Value is the **string**
  `"true"` or `"false"` (not a JSON boolean). Read it as:
  `config["clientConfig"].get("nsclient_watchdog_monitor") == "true"`

### Config Sync After Fresh Install (nsdiag -u)

After a fresh client install, `nsconfig.json` does **not** yet contain the
full tenant configuration (e.g. `configurationName` is missing). The client
must sync with the tenant first:

```
"C:\Program Files (x86)\Netskope\STAgent\nsdiag.exe" -u   # 32-bit client
"C:\Program Files\Netskope\STAgent\nsdiag.exe" -u          # 64-bit client
```

Wait ~30 seconds after running `nsdiag -u` for the config to be written to
`nsconfig.json`, then re-read it to get the correct `configurationName`.

### 64-bit Upgrade Flag (updateWin64Bit)

The `saveClientConfig` API accepts `updateWin64Bit` (0 or 1) to control
whether the tenant pushes 32-bit or 64-bit client installers during
auto-upgrade. Set via `update_client_config(updateWin64Bit=1)`.

### Upgrade Schedule (useScheduledUpgrade)

The tenant's `saveClientConfig` API accepts a `useScheduledUpgrade` field to
control when auto-upgrades are triggered:

```json
"useScheduledUpgrade": {
    "frequencyType": "daily",
    "weekDay": [],
    "weekOfTheMonth": [],
    "time": "16:54"
}
```

This is passed as a kwarg through `pylark-webapi-lib`'s
`ClientConfiguration.update_client_config()`, which already calls
`saveClientConfig` under the hood — no library changes needed.

## Git Workflow

- Main branch: `master`
- Create feature branches for new work
- Clear, descriptive commit messages
