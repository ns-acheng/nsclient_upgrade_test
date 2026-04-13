# Unit Test Backlog

Track UT coverage gaps here. Address in a dedicated batch pass.

## test_util_client.py

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
