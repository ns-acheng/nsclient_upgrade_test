"""
Unit tests for util_batch.py — normalize_argv, write_result_json,
try_record_manual_result.
All file I/O is mocked — no disk access occurs.
"""

import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from util_batch import (
    normalize_argv,
    write_result_json,
    try_record_manual_result,
    BatchRecord,
    TestRun,
    BATCH_RECORD_JSON,
    BATCH_RECORD_LOCAL_JSON,
)


# ── Helpers ───────────────────────────────────────────────────────────


def _make_result(
    success: bool = True,
    scenario: str = "upgrade_to_latest",
    version_before: str = "92.0.0",
    version_after: str = "95.0.0",
    expected_version: str = "95.0.0",
    webui_version: str = "95.0.0",
    elapsed_seconds: float = 120.0,
    message: str = "OK",
    critical_failure: bool = False,
) -> MagicMock:
    """Return a MagicMock shaped like UpgradeResult."""
    r = MagicMock()
    r.success = success
    r.scenario = scenario
    r.version_before = version_before
    r.version_after = version_after
    r.expected_version = expected_version
    r.webui_version = webui_version
    r.elapsed_seconds = elapsed_seconds
    r.message = message
    r.critical_failure = critical_failure
    return r


def _make_record(
    base_args: str = "upgrade --target latest",
    test_id: str = "t0",
    extra_args: str = "",
    status: str = "pending",
    version_before: str = "",
    version_after: str = "",
    log_dir: str = "",
) -> BatchRecord:
    """Return a BatchRecord with a single TestRun."""
    return BatchRecord(
        batch_id="20240101_000000",
        base_args=base_args,
        tests=[
            TestRun(
                id=test_id,
                extra_args=extra_args,
                status=status,
                version_before=version_before,
                version_after=version_after,
                log_dir=log_dir,
            )
        ],
    )


# ── normalize_argv ────────────────────────────────────────────────────


class TestNormalizeArgv:
    """Tests for normalize_argv() — pure logic, no I/O."""

    def test_empty_list(self) -> None:
        """Empty input produces an empty frozenset."""
        assert normalize_argv([]) == frozenset()

    def test_returns_frozenset(self) -> None:
        """Return type is always frozenset."""
        result = normalize_argv(["upgrade"])
        assert isinstance(result, frozenset)

    def test_single_plain_token(self) -> None:
        """A plain token that is not a meta-flag is kept."""
        assert normalize_argv(["upgrade"]) == frozenset({"upgrade"})

    def test_strips_config_flag_and_value(self) -> None:
        """--config and its value are both removed."""
        assert normalize_argv(["--config", "file.json", "upgrade"]) == frozenset({"upgrade"})

    def test_strips_tenant_flag_and_value(self) -> None:
        """--tenant and its value are both removed."""
        assert normalize_argv(["upgrade", "--tenant", "t.example.com"]) == frozenset({"upgrade"})

    def test_strips_username_flag_and_value(self) -> None:
        """--username and its value are both removed."""
        assert normalize_argv(["upgrade", "--username", "a@b.com"]) == frozenset({"upgrade"})

    def test_strips_password_flag_and_value(self) -> None:
        """--password and its value are both removed."""
        assert normalize_argv(["upgrade", "--password", "s3cret"]) == frozenset({"upgrade"})

    def test_strips_result_file_flag_and_value(self) -> None:
        """--result-file and its value are both removed."""
        assert normalize_argv(["upgrade", "--result-file", "out.json"]) == frozenset({"upgrade"})

    def test_strips_verbose_short(self) -> None:
        """-v is removed without consuming the next token."""
        assert normalize_argv(["upgrade", "-v"]) == frozenset({"upgrade"})

    def test_strips_verbose_long(self) -> None:
        """--verbose is removed without consuming the next token."""
        assert normalize_argv(["upgrade", "--verbose"]) == frozenset({"upgrade"})

    def test_preserves_non_meta_flag(self) -> None:
        """Flags that are not meta-flags are kept."""
        result = normalize_argv(["upgrade", "--target", "latest"])
        assert result == frozenset({"upgrade", "--target", "latest"})

    def test_order_insensitive(self) -> None:
        """Two lists with the same tokens in different order produce equal frozensets."""
        a = normalize_argv(["upgrade", "--target", "latest"])
        b = normalize_argv(["--target", "latest", "upgrade"])
        assert a == b

    def test_strips_multiple_meta_flags(self) -> None:
        """Multiple meta-flags are all stripped in one pass."""
        tokens = ["--config", "c.json", "-v", "--password", "x", "upgrade"]
        assert normalize_argv(tokens) == frozenset({"upgrade"})

    def test_trailing_meta_flag_no_value_no_error(self) -> None:
        """Meta-flag at the end with no following value does not raise."""
        result = normalize_argv(["upgrade", "--config"])
        assert result == frozenset({"upgrade"})


# ── write_result_json ─────────────────────────────────────────────────


class TestWriteResultJson:
    """Tests for write_result_json() — builtins.open and json.dump are mocked."""

    def _call_and_capture_data(
        self,
        result: MagicMock,
        log_dir=None,
        path: str = "out.json",
        started_at: str = "",
    ) -> dict:
        """Call write_result_json and return the dict passed to json.dump."""
        captured: list[dict] = []

        def fake_dump(data, fp, **kwargs):
            captured.append(data)

        m = mock_open()
        with patch("builtins.open", m), patch("util_batch.json.dump", side_effect=fake_dump):
            write_result_json(result, log_dir, path, started_at)

        assert len(captured) == 1, "json.dump should have been called exactly once"
        return captured[0]

    def test_writes_all_expected_fields(self) -> None:
        """Serialised dict contains every UpgradeResult field."""
        result = _make_result()
        data = self._call_and_capture_data(result, log_dir=Path("log/run1"))
        for key in (
            "success", "critical_failure", "scenario", "version_before",
            "version_after", "expected_version", "webui_version",
            "elapsed_seconds", "message", "log_dir", "started_at", "finished_at",
        ):
            assert key in data, f"Missing key: {key}"

    def test_log_dir_none_gives_empty_string(self) -> None:
        """log_dir=None serialises as empty string."""
        data = self._call_and_capture_data(_make_result(), log_dir=None)
        assert data["log_dir"] == ""

    def test_log_dir_path_gives_string(self) -> None:
        """log_dir Path serialises as a string."""
        data = self._call_and_capture_data(_make_result(), log_dir=Path("log/run1"))
        assert isinstance(data["log_dir"], str)
        assert "run1" in data["log_dir"]

    def test_started_at_passed_through(self) -> None:
        """started_at value appears unchanged in the output dict."""
        data = self._call_and_capture_data(
            _make_result(), started_at="2024-01-01T10:00:00",
        )
        assert data["started_at"] == "2024-01-01T10:00:00"

    def test_finished_at_is_set(self) -> None:
        """finished_at is populated (non-empty string)."""
        data = self._call_and_capture_data(_make_result())
        assert data["finished_at"]

    def test_success_field_matches_result(self) -> None:
        """success flag from result is written correctly."""
        data = self._call_and_capture_data(_make_result(success=False))
        assert data["success"] is False

    def test_critical_failure_field_matches_result(self) -> None:
        """critical_failure flag from result is written correctly."""
        data = self._call_and_capture_data(_make_result(critical_failure=True))
        assert data["critical_failure"] is True

    def test_newline_written_after_json(self) -> None:
        """A trailing newline is written after the JSON dump."""
        m = mock_open()
        with patch("builtins.open", m), patch("util_batch.json.dump"):
            write_result_json(_make_result(), None, "out.json")
        handle = m()
        written_args = [c.args[0] for c in handle.write.call_args_list]
        assert "\n" in written_args

    def test_open_exception_does_not_raise(self) -> None:
        """OSError from open() is swallowed and logged as a warning."""
        with patch("builtins.open", side_effect=OSError("disk full")), \
             patch("util_batch.log") as mock_log:
            write_result_json(_make_result(), None, "out.json")  # must not raise
        mock_log.warning.assert_called_once()


# ── try_record_manual_result ──────────────────────────────────────────


class TestTryRecordManualResult:
    """
    Tests for try_record_manual_result().

    Internal batch I/O functions are patched at the util_batch module level.
    No disk access occurs.
    """

    # ── helpers ──────────────────────────────────────────────────────

    def _run(
        self,
        argv: list[str],
        result: MagicMock = None,
        log_dir=None,
        started_at: str = "",
        *,
        record=None,
        batch_json_exists: bool = True,
        batch_config=None,
    ):
        """
        Call try_record_manual_result with patched internals.

        :param record: Value returned by load_record (None = no existing record).
        :param batch_json_exists: Simulates BATCH_JSON.exists().
        :param batch_config: (base_args, tests_list) returned by load_batch_config.
        """
        if result is None:
            result = _make_result()
        if batch_config is None:
            batch_config = ("upgrade --target latest", [{"id": "t0", "extra_args": ""}])

        # Replace the module-level Path constants with MagicMocks whose .exists()
        # returns the desired value.  Patching WindowsPath instances directly is
        # not supported by Python (attribute is read-only on Path objects).
        mock_batch_json = MagicMock(spec=Path)
        mock_batch_json.exists.return_value = batch_json_exists
        mock_batch_local_json = MagicMock(spec=Path)
        mock_batch_local_json.exists.return_value = batch_json_exists

        with patch("util_batch.load_record", return_value=record) as mock_load, \
             patch("util_batch.load_batch_config", return_value=batch_config) as mock_load_cfg, \
             patch("util_batch.create_record", side_effect=lambda ba, ts: _make_record(ba)) \
                 as mock_create, \
             patch("util_batch.apply_result_to_test") as mock_apply, \
             patch("util_batch.save_record") as mock_save, \
             patch("util_batch.generate_html_report") as mock_report, \
             patch("util_batch.BATCH_JSON", mock_batch_json), \
             patch("util_batch.BATCH_LOCAL_JSON", mock_batch_local_json):
            try_record_manual_result(result, log_dir, argv, started_at)
            return {
                "load_record": mock_load,
                "load_batch_config": mock_load_cfg,
                "create_record": mock_create,
                "apply_result_to_test": mock_apply,
                "save_record": mock_save,
                "generate_html_report": mock_report,
            }

    # ── early-exit cases ─────────────────────────────────────────────

    def test_no_record_and_no_batch_json_returns_early(self) -> None:
        """When no record and no batch.json, save_record is never called."""
        mocks = self._run(["upgrade"], record=None, batch_json_exists=False)
        mocks["save_record"].assert_not_called()

    def test_creates_record_when_only_batch_json_exists(self) -> None:
        """When record is missing but batch.json exists, create_record is called."""
        mocks = self._run(
            ["upgrade"],
            record=None,
            batch_json_exists=True,
            batch_config=("upgrade", [{"id": "t0", "extra_args": ""}]),
        )
        mocks["create_record"].assert_called_once()

    # ── matching ─────────────────────────────────────────────────────

    def test_no_argv_match_skips_save(self) -> None:
        """When no test in the record matches argv, save_record is not called."""
        record = _make_record(
            base_args="upgrade --target latest",
            extra_args="--reboot 5",
        )
        mocks = self._run(["upgrade", "--target", "golden"], record=record)
        mocks["save_record"].assert_not_called()

    # ── success result update rules ───────────────────────────────────

    def test_success_updates_pending_test(self) -> None:
        """A success result updates a test that is pending."""
        record = _make_record(
            base_args="upgrade --target latest",
            extra_args="",
            status="pending",
        )
        mocks = self._run(
            ["upgrade", "--target", "latest"],
            result=_make_result(success=True),
            record=record,
        )
        mocks["apply_result_to_test"].assert_called_once()
        mocks["save_record"].assert_called_once()

    def test_success_updates_already_passed_test(self) -> None:
        """A success result updates even a test already marked pass."""
        record = _make_record(
            base_args="upgrade --target latest",
            extra_args="",
            status="pass",
        )
        mocks = self._run(
            ["upgrade", "--target", "latest"],
            result=_make_result(success=True),
            record=record,
        )
        mocks["apply_result_to_test"].assert_called_once()

    # ── failure result update rules ───────────────────────────────────

    def test_failure_updates_pending_test(self) -> None:
        """A failure result updates a test that is pending."""
        record = _make_record(
            base_args="upgrade --target latest",
            extra_args="",
            status="pending",
        )
        mocks = self._run(
            ["upgrade", "--target", "latest"],
            result=_make_result(success=False),
            record=record,
        )
        mocks["apply_result_to_test"].assert_called_once()

    def test_failure_skips_passed_test(self) -> None:
        """A failure result does not overwrite a test already marked pass."""
        record = _make_record(
            base_args="upgrade --target latest",
            extra_args="",
            status="pass",
        )
        mocks = self._run(
            ["upgrade", "--target", "latest"],
            result=_make_result(success=False),
            record=record,
        )
        mocks["apply_result_to_test"].assert_not_called()
        mocks["save_record"].assert_not_called()

    def test_failure_updates_empty_fail_test(self) -> None:
        """A failure result updates a failed test that has no version/log data yet."""
        record = _make_record(
            base_args="upgrade --target latest",
            extra_args="",
            status="fail",
            version_before="",
            version_after="",
            log_dir="",
        )
        mocks = self._run(
            ["upgrade", "--target", "latest"],
            result=_make_result(success=False),
            record=record,
        )
        mocks["apply_result_to_test"].assert_called_once()

    def test_failure_skips_nonempty_fail_test(self) -> None:
        """A failure result does not overwrite a fail test that already has version data."""
        record = _make_record(
            base_args="upgrade --target latest",
            extra_args="",
            status="fail",
            version_before="92.0.0",
            version_after="92.0.0",
            log_dir="log/run1",
        )
        mocks = self._run(
            ["upgrade", "--target", "latest"],
            result=_make_result(success=False),
            record=record,
        )
        mocks["apply_result_to_test"].assert_not_called()

    # ── local-target routing ──────────────────────────────────────────

    def test_local_target_routes_to_local_record_path(self) -> None:
        """argv containing --target local causes load_record to use BATCH_RECORD_LOCAL_JSON."""
        mocks = self._run(
            ["upgrade", "--target", "local"],
            record=_make_record(base_args="upgrade --target local"),
        )
        call_path = mocks["load_record"].call_args[0][0]
        assert call_path == BATCH_RECORD_LOCAL_JSON

    def test_default_target_routes_to_standard_record_path(self) -> None:
        """argv without --target local causes load_record to use BATCH_RECORD_JSON."""
        mocks = self._run(
            ["upgrade", "--target", "latest"],
            record=_make_record(base_args="upgrade --target latest"),
        )
        call_path = mocks["load_record"].call_args[0][0]
        assert call_path == BATCH_RECORD_JSON

    # ── post-save actions ─────────────────────────────────────────────

    def test_generates_html_report_after_save(self) -> None:
        """generate_html_report is called after save_record on a successful match."""
        record = _make_record(
            base_args="upgrade --target latest",
            extra_args="",
            status="pending",
        )
        mocks = self._run(
            ["upgrade", "--target", "latest"],
            result=_make_result(success=True),
            record=record,
        )
        mocks["generate_html_report"].assert_called_once()

    # ── exception safety ─────────────────────────────────────────────

    def test_exception_in_load_record_does_not_propagate(self) -> None:
        """Any exception inside try_record_manual_result is silently swallowed."""
        with patch("util_batch.load_record", side_effect=Exception("boom")), \
             patch("util_batch.log") as mock_log:
            try_record_manual_result(_make_result(), None, ["upgrade"])
        mock_log.debug.assert_called()
