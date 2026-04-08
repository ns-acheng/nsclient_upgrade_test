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

## Git Workflow

- Main branch: `master`
- Create feature branches for new work
- Clear, descriptive commit messages
