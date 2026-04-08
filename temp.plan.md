# Plan: Reboot-Interrupt Upgrade Test

## Context

The test plan (`doc/test_plan.md`) defines upgrade resilience scenarios (5.2–5.5)
that require forcing a reboot mid-upgrade and verifying the endpoint recovers.
The current tool can only run normal (uninterrupted) upgrades. We need:

1. **Post-reboot verification** — check services, driver, install paths, version
2. **Reboot-interrupt orchestration** — trigger reboot at configurable timing
3. **32/64-bit upgrade path awareness** — verify correct install directory

A reboot kills the tool process, so this is inherently a **two-phase** flow:
- **Phase 1 (`reboot-setup`)**: install base, enable upgrade, save state, trigger reboot
- **Phase 2 (`reboot-verify`)**: read saved state, wait for stabilization, run all checks

---

## Files to Modify

| File | Change |
|---|---|
| `util_client.py` | Add service query helpers (state, binpath, driver) and install path helpers |
| `upgrade_runner.py` | Add `run_reboot_interrupt_setup()` (phase 1) and `run_reboot_verify()` (phase 2) |
| `main.py` | Add `reboot-setup` and `reboot-verify` CLI subcommands |
| `util_config.py` | Add `RebootTestState` dataclass for reboot state persistence |
| `test/test_upgrade_runner.py` | Tests for new scenarios |
| `test/test_util_client.py` | Tests for new service query methods |

---

## 1. Service & Install Path Queries (`util_client.py`)

### 1a. New constants

```python
INSTALL_DIR_32 = Path(r"C:\Program Files (x86)\Netskope\STAgent")
INSTALL_DIR_64 = Path(r"C:\Program Files\Netskope\STAgent")

# Services to verify after upgrade
SERVICES = {
    "client":   "stAgentSvc",
    "watchdog": "stwatchdog",
    "driver":   "stadrv",
}
```

### 1b. `query_service(service_name) -> dict`

Runs `sc query <name>`, parses output into:
```python
{"name": str, "exists": bool, "state": str}  # state = "RUNNING" / "STOPPED" / ...
```

### 1c. `query_service_binpath(service_name) -> str`

Runs `sc qc <name>`, parses `BINARY_PATH_NAME` from output.
Used to verify watchdog points to the correct install directory.

### 1d. `get_install_dir(is_64_bit: bool) -> Path`

Returns `INSTALL_DIR_64` or `INSTALL_DIR_32` based on target bitness.

### 1e. `verify_install_dir(is_64_bit: bool) -> bool`

Checks that expected install dir exists and contains key files
(e.g. `stAgentSvc.exe`).

---

## 2. Reboot State Persistence (`util_config.py`)

### 2a. `RebootTestState` dataclass

```python
@dataclass
class RebootTestState:
    scenario: str           # "reboot_interrupt"
    version_before: str     # e.g. "135.0.0.2631"
    target_type: str        # "latest" or "golden"
    expected_version: str   # e.g. "136.0.4.2612"
    reboot_timing: str      # "early" / "mid" / "late" / "<N>s"
    source_64_bit: bool     # was the base install 64-bit?
    target_64_bit: bool     # is the upgrade target 64-bit?
    config_name: str        # tenant config name
    timestamp: str          # ISO timestamp when setup ran
```

### 2b. Save/load helpers

- `save_reboot_state(state) -> Path` — writes `data/reboot_state.json`
- `load_reboot_state() -> RebootTestState | None` — reads it back
- `clear_reboot_state()` — deletes after verify completes

---

## 3. Phase 1: `run_reboot_interrupt_setup()` (`upgrade_runner.py`)

Flow:
1. `_ensure_client_installed()` + `_sync_and_detect_config()`
2. `_init_nsclient()`, read `version_before`
3. Get expected version from WebUI (latest or golden)
4. `disable_auto_upgrade()` then `enable_upgrade_latest/golden()`
5. `set_upgrade_schedule(minutes_from_now=2)`
6. `client.update_config()` to pull new config
7. Save `RebootTestState` to `data/reboot_state.json`
8. Convert reboot_timing to seconds:
   - `"early"` → 30s (during download/prep)
   - `"mid"` → 60s (during old service removal)
   - `"late"` → 90s (during new service/driver install)
   - `"<N>"` → N seconds (custom)
9. Execute `shutdown /r /f /t <delay>`
10. Return setup result (not waiting for upgrade — the reboot will interrupt it)

---

## 4. Phase 2: `run_reboot_verify()` (`upgrade_runner.py`)

Flow (runs after reboot, reads saved state):
1. Load `RebootTestState` from `data/reboot_state.json`
2. Wait for system stabilization (default 300s = 5 min, configurable)
3. Run comprehensive verification:

### 4a. Service checks
For each service (`stAgentSvc`, `stwatchdog`, `stadrv`):
- `query_service()` → exists? running?
- Log state

### 4b. Watchdog binary path
- `query_service_binpath("stwatchdog")`
- Verify path points to the correct install dir based on `target_64_bit`
- If upgrade rolled back, path should match `source_64_bit` instead

### 4c. Version check
- Read installed version (nsclient or WebUI fallback)
- Must be either `version_before` (rollback) or `expected_version` (completed)
- NOT "unknown" or missing

### 4d. Install path verification
- Based on the detected version, determine which bitness is active
- Verify that install dir exists and contains expected binaries
- Verify the *other* install dir is clean (no orphaned files from wrong bitness)

### 4e. Result

```python
@dataclass
class RebootVerifyResult:
    success: bool
    scenario: str
    version_before: str
    version_after: str
    expected_version: str
    upgrade_completed: bool      # True if version_after == expected
    rolled_back: bool            # True if version_after == version_before
    services: dict[str, dict]    # {name: {exists, state}}
    watchdog_binpath: str
    watchdog_binpath_valid: bool
    install_dir_valid: bool
    elapsed_seconds: float
    message: str
```

**Success criteria** (from test_plan.md Section 7):
- At least one working version present (old OR new)
- All three services exist and are running
- Watchdog binary path is valid and correct
- No "missing client" state

---

## 5. CLI Commands (`main.py`)

### `reboot-setup`
```
python main.py reboot-setup --target latest --reboot-timing mid [--64bit] [--target-64bit]
```

Arguments:
- `--target`: `latest` or `golden` (required)
- `--reboot-timing`: `early`/`mid`/`late`/`<N>` (required)
- `--from-version`: base version for download fallback
- `--64bit`: source install is 64-bit
- `--target-64bit`: upgrade target is 64-bit
- `--dot`: enable dot release (golden only)
- `--email`: send enrollment invite
- `--stabilize-wait`: seconds to wait in verify phase (saved to state, default 300)

### `reboot-verify`
```
python main.py reboot-verify [--stabilize-wait 300]
```

Reads everything from `data/reboot_state.json`. Minimal args needed.

---

## 6. 32/64-bit Upgrade Path Matrix

Four combinations, each verified by checking the install directory:

| Source | Target | Expected install dir after upgrade |
|--------|--------|------------------------------------|
| 32-bit | 32-bit | `C:\Program Files (x86)\Netskope\STAgent` |
| 32-bit | 64-bit | `C:\Program Files\Netskope\STAgent` |
| 64-bit | 32-bit | `C:\Program Files (x86)\Netskope\STAgent` |
| 64-bit | 64-bit | `C:\Program Files\Netskope\STAgent` |

On rollback, expected dir matches the **source** bitness instead.

The `--64bit` flag controls source, `--target-64bit` controls target.
Both are saved in `RebootTestState` so `reboot-verify` knows what to check.

---

## 7. Verification

- Run `python -m pytest test/ -v` — all existing + new tests pass
- Manual dry-run:
  1. `python main.py reboot-setup --target latest --reboot-timing mid` (triggers reboot)
  2. After reboot: `python main.py reboot-verify` (checks everything)
