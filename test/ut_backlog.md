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
