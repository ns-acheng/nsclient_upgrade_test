# Unit Test Backlog

Track UT coverage gaps here. Address in a dedicated batch pass.

## test_util_client.py

- **_collect_event_logs success**: Mock `subprocess.run` returning exit code 0 for both System and Application channels; verify `wevtutil.exe epl <channel> <file>` is called and success is logged.
- **_collect_event_logs failure**: Mock `subprocess.run` returning non-zero exit code; verify a warning is logged and no exception is raised.
- **_collect_event_logs exception**: Mock `subprocess.run` raising `Exception`; verify warning is logged and no exception propagates.
- **collect_log_bundle calls _collect_event_logs**: Verify `_collect_event_logs` is called with `output_dir` and the generated timestamp before nsdiag is invoked.
- **collect_log_bundle event logs use same timestamp**: Verify the timestamp passed to `_collect_event_logs` matches the one used for the log bundle filename.

---


- **uninstall_msi log_dir parameter**: `uninstall_msi` now accepts `log_dir` and passes `/l*v` to msiexec. Tests that call `assert_called_once_with` need the new `log_dir=` kwarg (lines 880, 911).
- **uninstall_msi 1603 raises UninstallCriticalError**: After both attempts fail with exit code 1603, `UninstallCriticalError` is raised instead of `RuntimeError`. Add a test that mocks `subprocess.run` returning 1603 twice and asserts `UninstallCriticalError`.
- **uninstall_msi non-1603 still raises RuntimeError**: Verify other exit codes still raise plain `RuntimeError`.
- **uninstall_msi msiexec_uninstall.log path**: When `log_dir` is provided, verify the msiexec command includes `/l*v <log_dir>/msiexec_uninstall.log`.

## test_upgrade_runner.py

- **critical_failure on UninstallCriticalError**: All three scenario except blocks (`run_upgrade_to_latest`, `run_upgrade_to_golden`, `run_upgrade_disabled`) now set `critical_failure=True` when the exception is `UninstallCriticalError`. Add tests that mock `ensure_client_installed` raising `UninstallCriticalError` and verify the returned `UpgradeResult.critical_failure is True`.
- **non-critical exception still has critical_failure=False**: Verify a generic `RuntimeError` from `ensure_client_installed` still returns `critical_failure=False`.
- **uninstall_msi assert_called_once_with**: Lines 880, 911 need `log_dir=None` kwarg added.

## test_util_monitor.py — standby feature

- **`_trigger_standby` idempotency**: Call `_trigger_standby()` twice on the same monitor; mock `enter_s1_and_wake`. Verify the power API is called exactly once (second call is a no-op via `standby_triggered` guard).
- **`_trigger_standby` s0 path**: Pass `standby="s0"` to `TimingMonitor`; verify `enter_s0_and_wake` is called (not `enter_s1_and_wake`).
- **`_trigger_standby` s1 path**: Pass `standby="s1"`; verify `enter_s1_and_wake` is called.
- **`_trigger_standby` ImportError**: Mock `tool.power_api` import to raise `ImportError`; verify error is logged and no exception propagates.
- **`_trigger_standby` power API failure (returns False)**: Mock the API to return `False`; verify warning is logged and no exception propagates.
- **`_run()` standby branch — no return**: With `standby="s1"` and `reboot_time=N`, mock `_trigger_standby`; verify the monitor thread does NOT exit after the timed timing fires (continues processing subsequent timings).
- **`_run()` reboot branch still returns**: Verify that without `standby`, `_trigger_reboot` is called and the thread exits as before.
- **`wait_for_upgrade_complete` timing-1 fast-path skipped for standby**: Set `reboot_time=1`, `standby="s1"`; verify `_trigger_reboot` is NOT called from `wait_for_upgrade_complete` (fast-path guarded by `not self._state.standby`).
- **`MonitorState` serialisation round-trip with standby**: Create a state with `standby="s0"`, serialise with `asdict`, reconstruct with `MonitorState(**data)`; verify `standby == "s0"` and `standby_triggered == False`.
- **`STANDBY_WAKE_SECONDS` passed to API**: Verify the power API is called with `STANDBY_WAKE_SECONDS` (currently 30).
- **`STANDBY_WAKE_SECONDS` passed to API**: Verify the power API is called with `STANDBY_WAKE_SECONDS` (currently 30).

## test_util_email.py — Browser Automation Testing Mocks

**Issue**: Email browser tests (`test_get_download_link_success`, `test_unwraps_google_redirect_in_link`, `test_retries_on_no_results`, `test_timeout_raises`) attempt to mock Selenium WebDriver but the mocking is incomplete. The actual `get_download_link()` method code executes with partial mocks, leading to test failures.

**Root Cause**: `WebDriverWait(driver, 15)` instances are created fresh inside the method, but test mocks don't properly configure:
- `driver.get()`, `driver.current_url` property
- `driver.execute_script()` return value
- `EC.presence_of_element_located()` and related expected conditions
- Helper method returns (`_find_search_box()`, `_extract_link_from_body()`)

**Fix Required** (dedicated browser testing pass):
- Mock all `driver` method calls comprehensively in test setup
- For each test, patch helper methods (`_find_search_box`, `_extract_link_from_body`, `_dismiss_overlays`) at class or method level
- Ensure WebDriverWait mock returns appropriate values for sequential `.until()` calls
- Update error message expectation in `test_timeout_raises` from "Email not found" to "No unread email found within"

## test_batch.py / test_util_batch.py — New batch profile + merge plan

- **`--local` profile selection**: Verify `batch.py --local` uses `data/batch_local.json`, writes `log/batch_record_local.json`, and generates `log/batch_report_local.html`.
- **default profile selection**: Verify no `--local` keeps using `data/batch.json`, `log/batch_record.json`, and `log/batch_report.html`.
- **removed path flags enforcement**: Verify legacy `--batch` / `--record` are rejected by CLI parser.
- **`--merge` requires inputs**: Verify `python batch.py --merge` without files returns error.
- **merge positional guard**: Verify positional record files without `--merge` return error.
- **merge creates target record if missing**: With no existing target record, verify merge initializes from selected batch config and then applies source results.
- **merge by test id only**: Verify unknown IDs from source records are skipped (counted as skipped) and do not create new test entries.
- **merge when target has no data**: Verify source result is copied into target when target test is empty/pending.
- **merge conflict resolution by timestamp**: For same test id with data in both target and source, keep newer result by `finished_at` (fallback: `started_at`).
- **merge ignores older source**: Verify older source record does not overwrite newer target result.
- **merge multiple source files order-independence**: Verify final result is still newest-by-timestamp regardless of input file order.
- **merge regenerates report**: Verify merge always writes corresponding HTML report path for selected profile.

## test_util_installer.py — No-client cleanup hook plan

- **no-client branch runs cleanup**: When uninstall entry is not found, verify `_run_cleanup_batch()` is invoked before install flow continues.
- **installed branch skips cleanup hook**: When uninstall entry exists, verify uninstall path executes and cleanup hook is not called.
- **cleanup script missing**: Verify `_run_cleanup_batch()` logs warning and returns without raising.
- **cleanup non-zero exit is non-fatal**: Mock `subprocess.run` return code != 0 and verify warning is logged but install flow proceeds.
- **cleanup exception is non-fatal**: Mock `subprocess.run` raising exception and verify warning is logged and install flow proceeds.
- **cleanup command shape**: Verify execution uses `cmd.exe /c <repo>/tool/cleanup.bat`.
