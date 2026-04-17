"""
Batch runner for Netskope Client auto-upgrade tests.

Usage:
    python batch.py                  Run all pending tests from data/batch.json
    python batch.py --local          Run local profile (data/batch_local.json)
    python batch.py --resume         Resume an existing batch run
    python batch.py --continue       Resume after reboot (called by scheduled task)
    python batch.py --report         Re-generate HTML report from existing record
    python batch.py --merge record1.json record2.json
    python batch.py --merge --local record1.json record2.json
    python batch.py --fresh          Discard record and start fresh

Options:
    --local          Use local profile paths
    --retry-unknown  Reset failed tests with empty/unknown/N/A version_before to pending
    -v               Verbose logging
"""

import argparse
import logging
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from util_batch import (
    BATCH_JSON,
    BATCH_RECORD_JSON,
    BATCH_LOCAL_JSON,
    BATCH_RECORD_LOCAL_JSON,
    BatchRecord,
    TestRun,
    apply_result_to_test,
    create_record,
    delete_batch_continue_task,
    generate_html_report,
    has_reboot,
    load_batch_config,
    load_record,
    read_result_file,
    register_batch_continue_task,
    run_test_subprocess,
    save_record,
)
from util_input import start_input_monitor
from util_log import setup_batch_logging, setup_logging

log = logging.getLogger(__name__)

_GREEN = "\033[92m"
_RED   = "\033[91m"
_RESET = "\033[0m"


# ── Helpers ───────────────────────────────────────────────────────────


def _selected_paths(local: bool) -> tuple[Path, Path]:
    """Return (batch_path, record_path) based on --local profile."""
    if local:
        return BATCH_LOCAL_JSON, BATCH_RECORD_LOCAL_JSON
    return BATCH_JSON, BATCH_RECORD_JSON


def _report_path_for(record_path: Path) -> Path:
    """Return report path matching the selected record file."""
    report_name = record_path.stem.replace("batch_record", "batch_report") + ".html"
    return record_path.parent / report_name


def _result_file_for(record: BatchRecord, test: TestRun) -> Path:
    """Derive a per-test result JSON path inside the log directory."""
    return BATCH_RECORD_JSON.parent / f"result_{record.batch_id}_{test.id}.json"


def _next_pending_index(record: BatchRecord) -> int:
    """Return index of the first pending/running test, or -1 if all done."""
    for i, t in enumerate(record.tests):
        if t.status in ("pending", "running"):
            return i
    return -1


def _next_pending_index_excluding(
    record: BatchRecord,
    deferred_ids: set[str],
) -> int:
    """
    Return index of the first pending/running test not in *deferred_ids*.

    This is used to defer known non-upgrade external failures (e.g. Gmail
    download-link extraction) so the batch can continue to the next upgrade
    test without re-running the same deferred case immediately.
    """
    for i, t in enumerate(record.tests):
        if t.status in ("pending", "running") and t.id not in deferred_ids:
            return i
    return -1


def _is_email_link_related_failure(message: str) -> bool:
    """
    Return True when a failure message indicates Gmail invite-link extraction
    failed (external/non-upgrade dependency failure).
    """
    msg = (message or "").lower()
    if not msg:
        return False
    return (
        "email invite flow" in msg
        and "download link" in msg
        and "gmail" in msg
    )


def _is_stopped_by_user_failure(message: str) -> bool:
    """Return True when a failure message indicates user-stop/ESC."""
    msg = (message or "").lower()
    return "stopped by user" in msg


def _is_unknown_version_before_failure(test: TestRun) -> bool:
    """
    Return True when a failed test never reached upgrade validation.

    This uses version_before markers that indicate setup/install phase
    failure before we can collect a meaningful source version.
    """
    return (
        test.status == "fail"
        and test.version_before in ("", "unknown", "N/A")
    )


def _mark_test_pending_for_retake(test: TestRun, message: str) -> None:
    """Reset result fields and mark a test pending for later retry."""
    test.status = "pending"
    test.critical_failure = False
    test.log_dir = ""
    test.version_before = ""
    test.version_after = ""
    test.expected_version = ""
    test.elapsed_seconds = 0.0
    test.finished_at = ""
    test.message = message


def _print_test_result(test: TestRun) -> None:
    color = _GREEN if test.status == "pass" else _RED
    elapsed = f"  ({test.elapsed_seconds:.0f}s)" if test.elapsed_seconds else ""
    print(f"  [{color}{test.status.upper()}{_RESET}] {test.id}{elapsed}: {test.message or '—'}")


def _print_summary(record: BatchRecord) -> None:
    n_pass = sum(1 for t in record.tests if t.status == "pass")
    n_fail = sum(1 for t in record.tests if t.status == "fail")
    n_pend = sum(1 for t in record.tests if t.status == "pending")
    total = len(record.tests)
    print(f"\n{'=' * 55}")
    print(f"  Batch {record.batch_id} complete")
    print(
        f"  {_GREEN}PASS {n_pass}{_RESET}  "
        f"{_RED}FAIL {n_fail}{_RESET}  "
        f"SKIP {n_pend}  / {total}"
    )
    print(f"{'=' * 55}")


# ── Core execution loop ───────────────────────────────────────────────


def _execute_pending(record: BatchRecord, record_path: Path) -> int:
    """
    Run all pending tests in *record* sequentially.

    Saves the record after every test so progress is never lost.
    For reboot tests, registers the batch continue task BEFORE
    launching the subprocess — if the machine reboots, the task
    fires after login and resumes via ``batch.py --continue``.

    Starts an ESC key monitor; pressing ESC terminates the current
    test subprocess and stops the batch.

    :return: 0 if all tests passed, 1 if any failed or batch was stopped.
    """
    report_path = _report_path_for(record_path)
    stop_event = threading.Event()
    start_input_monitor(stop_event)
    deferred_ids: set[str] = set()

    while True:
        if stop_event.is_set():
            log.warning("Batch stopped by user (ESC).")
            print("\nBatch stopped by user (ESC).")
            record.finished_at = datetime.now().isoformat(timespec="seconds")
            save_record(record, record_path)
            generate_html_report(record, report_path)
            return 1

        idx = _next_pending_index_excluding(record, deferred_ids)
        if idx < 0:
            # If only deferred tests are left, finish this batch pass and leave
            # those tests as pending for a later retry/resume.
            if deferred_ids and _next_pending_index(record) >= 0:
                ids = ", ".join(sorted(deferred_ids))
                log.warning(
                    "Deferred external email-link failures kept pending: %s",
                    ids,
                )
                print(
                    "\nDeferred external email-link failure(s) left as pending: "
                    f"{ids}"
                )
            record.finished_at = datetime.now().isoformat(timespec="seconds")
            save_record(record, record_path)
            out = generate_html_report(record, report_path)
            _print_summary(record)
            print(f"\nReport: {out}")
            all_pass = all(t.status == "pass" for t in record.tests)
            return 0 if all_pass else 1

        test = record.tests[idx]
        result_file = _result_file_for(record, test)
        total = len(record.tests)

        print(f"\n[{idx + 1}/{total}] {test.id}")
        if test.extra_args:
            print(f"  Extra args: {test.extra_args}")

        # Register continue task BEFORE starting a reboot test so
        # the task is already in place when the machine reboots.
        if has_reboot(test.extra_args):
            register_batch_continue_task(
                local=(record_path == BATCH_RECORD_LOCAL_JSON)
            )

        # Persist "running" status to disk BEFORE launching the subprocess.
        # If the machine reboots during the test, batch.py is killed and
        # save_record() below never runs — the disk copy would still show
        # "pending" and batch.py --continue would re-run the test from
        # scratch instead of resuming it.
        test.status = "running"
        test.started_at = datetime.now().isoformat(timespec="seconds")
        save_record(record, record_path)

        run_test_subprocess(record.base_args, test, result_file, stop_event)
        save_record(record, record_path)

        # Treat external/non-upgrade dependency failures (and unknown
        # version_before failures) as deferred pending and move to next
        # test in this pass.
        is_email_failure = _is_email_link_related_failure(test.message)
        is_stopped_failure = _is_stopped_by_user_failure(test.message)
        is_unknown_before = _is_unknown_version_before_failure(test)

        if is_email_failure or is_stopped_failure or is_unknown_before:
            if is_email_failure:
                defer_reason = "email-link extraction failure"
                pending_note = (
                    "Deferred: email invite/link extraction failed; "
                    "pending for later retry"
                )
            elif is_stopped_failure:
                defer_reason = "stopped-by-user failure"
                pending_note = (
                    "Deferred: stopped by user; "
                    "pending for later retry"
                )
            else:
                defer_reason = "unknown version_before"
                pending_note = (
                    "Deferred: version_before unknown; "
                    "pending for later retry"
                )
            log.warning(
                "[%s] Deferred as pending due to %s",
                test.id,
                defer_reason,
            )
            # Keep start timestamp as audit trail but clear result payload.
            _mark_test_pending_for_retake(test, pending_note)
            deferred_ids.add(test.id)
            save_record(record, record_path)
            generate_html_report(record, report_path)
            print(
                f"  [PENDING] {test.id}: deferred ({defer_reason})"
            )

            # This failure type is intentionally deferred and should not force
            # a full batch stop in the next loop iteration.
            if is_stopped_failure and stop_event.is_set():
                stop_event.clear()
                start_input_monitor(stop_event)

            continue

        # If the test is still "running", the monitor triggered a
        # reboot and the subprocess was killed by Windows shutdown.
        # The batch continue task is already registered; just save the
        # record and wait for the reboot to kill this process.
        if test.status == "running":
            log.info(
                "Reboot pending for [%s] — waiting for shutdown.",
                test.id,
            )
            print(f"\n  Reboot triggered for {test.id} — waiting for shutdown...")
            time.sleep(120)
            # Should not reach here — OS kills the process.
            return 1

        _print_test_result(test)

        # Critical post-upgrade validation failure — stop immediately.
        if test.critical_failure:
            log.error(
                "Critical post-upgrade validation failure in [%s] — stopping batch.",
                test.id,
            )
            print(f"\n{'!' * 55}")
            print(f"  CRITICAL: Post-upgrade validation failed in {test.id}")
            print(f"  Stopping batch. Remaining tests left as pending.")
            print(f"{'!' * 55}")
            record.finished_at = datetime.now().isoformat(timespec="seconds")
            save_record(record, record_path)
            generate_html_report(record, report_path)
            return 1

        # If we reach here the subprocess finished without rebooting.
        # Remove the continue task we registered above.
        if has_reboot(test.extra_args):
            delete_batch_continue_task()

        # Regenerate report after every test so partial results are
        # always readable.
        generate_html_report(record, report_path)


# ── Commands ──────────────────────────────────────────────────────────


def cmd_run(args: argparse.Namespace) -> int:
    """
    Run all pending tests.

    With ``--resume``, load the existing record and skip completed
    tests.  Without it, start fresh — but if a record already exists,
    prompt the user to overwrite or back it up first.

    With ``--retry-failed`` or ``--retry ID``, load the existing record,
    reset the target tests to pending, and re-run.
    """
    batch_path, record_path = _selected_paths(args.local)

    # ── Retry mode ────────────────────────────────────────────────────
    if args.retry_failed or args.retry or args.retry_unknown:
        record = load_record(record_path)
        if record is None:
            print("Error: No batch record found. Run batch.py first.")
            return 1
        n = _reset_tests(
            record,
            retry_failed=args.retry_failed,
            retry_ids=args.retry or [],
            retry_unknown_version=args.retry_unknown,
        )
        if n == 0:
            print("No matching tests found to retry.")
            return 0
        save_record(record, record_path)
        print(f"\nBatch {record.batch_id} — {n} test(s) reset to pending")
        print(f"Base args: {record.base_args}\n")
        return _execute_pending(record, record_path)

    # ── Resume mode ───────────────────────────────────────────────────
    if args.resume:
        record = load_record(record_path)
        if record:
            auto_reset = 0
            for test in record.tests:
                if _is_unknown_version_before_failure(test):
                    _mark_test_pending_for_retake(
                        test,
                        "Deferred: version_before unknown; pending for later retry",
                    )
                    auto_reset += 1
            if auto_reset:
                save_record(record, record_path)
                log.info(
                    "Auto-reset %d unknown-version failure(s) to pending",
                    auto_reset,
                )
            log.info(
                "Resuming batch %s — %d tests total",
                record.batch_id, len(record.tests),
            )
        else:
            log.info("No existing record found — starting fresh")
            record = None
    else:
        record = None
        if record_path.exists():
            record = _prompt_overwrite_or_backup(record_path)

    if record is None:
        base_args, tests = load_batch_config(batch_path)
        record = create_record(base_args, tests)
        save_record(record, record_path)
        log.info(
            "New batch %s created — %d tests",
            record.batch_id, len(record.tests),
        )

    print(f"\nBatch {record.batch_id} — {len(record.tests)} tests")
    print(f"Base args: {record.base_args}\n")

    return _execute_pending(record, record_path)


def _reset_test(test: TestRun) -> None:
    """Clear all result fields and set status back to pending."""
    test.status = "pending"
    test.log_dir = ""
    test.version_before = ""
    test.version_after = ""
    test.expected_version = ""
    test.elapsed_seconds = 0.0
    test.message = ""
    test.started_at = ""
    test.finished_at = ""


def _reset_tests(
    record: BatchRecord,
    retry_failed: bool = False,
    retry_ids: list[str] | None = None,
    retry_unknown_version: bool = False,
) -> int:
    """
    Reset tests to pending in-place.

    :param retry_failed: Reset every test whose status is ``fail``.
    :param retry_ids: Reset tests whose id is in this list (any status).
    :param retry_unknown_version: Reset failed tests whose ``version_before``
                                  is empty, ``"unknown"``, or ``"N/A"`` —
                                  indicates the test failed before or during
                                  base MSI installation and never reached the
                                  upgrade phase.
    :return: Number of tests reset.
    """
    ids = set(retry_ids or [])
    n = 0
    for test in record.tests:
        unknown_ver = (
            retry_unknown_version
            and test.status == "fail"
            and test.version_before in ("", "unknown", "N/A")
        )
        if (retry_failed and test.status == "fail") or test.id in ids or unknown_ver:
            _reset_test(test)
            log.info("Reset test [%s] to pending", test.id)
            n += 1

    unknown = ids - {t.id for t in record.tests}
    for uid in sorted(unknown):
        print(f"  Warning: test ID '{uid}' not found in record")
    return n


def _prompt_overwrite_or_backup(record_path: Path) -> Optional[BatchRecord]:
    """
    Prompt the user when a batch record already exists.

    - ``o`` (default): overwrite — return None so caller creates fresh.
    - ``b``: back up existing file with a timestamp suffix, then return
             None so caller creates a fresh record.

    :return: None in both cases (caller always creates a fresh record).
    """
    existing = load_record(record_path)
    if existing:
        n_done = sum(1 for t in existing.tests if t.status in ("pass", "fail"))
        total = len(existing.tests)
        print(
            f"\n  Existing batch found: {existing.batch_id} "
            f"({n_done}/{total} complete)"
        )
    else:
        print(f"\n  {record_path.name} already exists.")

    print("  [o] Overwrite  [b] Backup and start fresh  (default: o)")
    try:
        choice = input("  Choice: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        choice = "o"

    if choice == "b":
        suffix = existing.batch_id if existing else datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = record_path.with_name(f"{record_path.stem}_{suffix}.json")
        record_path.rename(backup)
        print(f"  Backed up to {backup.name}")
        log.info("Backed up existing record to %s", backup)
    else:
        print("  Overwriting existing record.")

    return None


def cmd_continue(args: argparse.Namespace) -> int:
    """
    Resume after reboot.

    1. Delete the batch continue scheduled task.
    2. Delete any stale NsClientMonitorContinue task (batch runner owns
       post-reboot logic; the monitor continue task is not used in batch mode).
    3. Find the interrupted (running) test in the record.
    4. If ``main.py continue`` monitor state exists, run it and wait.
    5. Apply the result to the test record.
    6. Continue with remaining pending tests.
    """
    _, record_path = _selected_paths(args.local)
    record = load_record(record_path)
    if record is None:
        print("Error: No batch record found. Nothing to continue.")
        return 1

    delete_batch_continue_task()

    # Clean up any stale NsClientMonitorContinue task. In batch mode the
    # batch runner calls main.py continue directly; a leftover task from
    # a prior non-batch run (or an interrupted run) firing on this logon
    # would just show an error console and confuse the user.
    from util_monitor import delete_continue_task as _delete_monitor_task
    _delete_monitor_task()

    # Find the interrupted test (status = running)
    running_idx = next(
        (i for i, t in enumerate(record.tests) if t.status == "running"),
        -1,
    )

    if running_idx >= 0:
        test = record.tests[running_idx]
        result_file = _result_file_for(record, test)
        log.info("Resuming interrupted test [%s]", test.id)

        # Check if main.py continue (monitor state) is still pending
        monitor_state = _load_monitor_state_safe()
        if monitor_state is not None:
            log.info("Monitor state found — running main.py continue")
            _run_main_continue(result_file)

        result = read_result_file(result_file)
        if result:
            apply_result_to_test(test, result)
        else:
            test.status = "fail"
            test.message = "No result file after reboot continue"
            test.finished_at = datetime.now().isoformat(timespec="seconds")

        save_record(record, record_path)
        _print_test_result(test)

    return _execute_pending(record, record_path)


def cmd_report(args: argparse.Namespace) -> int:
    """Re-generate the HTML report from an existing record."""
    _, record_path = _selected_paths(args.local)
    record = load_record(record_path)
    if record is None:
        print("Error: No batch record found.")
        return 1
    report_path = _report_path_for(record_path)
    out = generate_html_report(record, report_path)
    print(f"Report generated: {out}")
    return 0


def cmd_fresh(args: argparse.Namespace) -> int:
    """
    Clean old batch data by backing up and resetting the record.

    This command backs up the existing record to a fixed backup file,
    then resets all test statuses to pending (clears results but keeps
    the file). It also regenerates the HTML report showing all tests
    as pending and ready to run.
    """
    batch_path, record_path = _selected_paths(args.local)
    report_path = _report_path_for(record_path)

    print("\n" + "=" * 55)
    print("  Batch Fresh — Backup & Reset")
    print("=" * 55)

    # Check if record exists and reset it
    if not record_path.exists():
        print(f"\n  [OK] {record_path.name} — not found (already clean)")
        record = _backup_and_reset_record(batch_path, record_path)
    else:
        record = load_record(record_path)
        if record:
            n_tests = len(record.tests)
            n_done = sum(1 for t in record.tests if t.status in ("pass", "fail"))
            print(
                f"\n  Cleaning {record_path.name}:"
                f"\n    - Batch ID: {record.batch_id}"
                f"\n    - Tests: {n_done}/{n_tests} complete"
            )
        else:
            print(f"\n  Cleaning {record_path.name}:")
            print("    - Existing record is invalid/corrupt; recreating from batch config")
        record = _backup_and_reset_record(batch_path, record_path)

    # Regenerate the HTML report from the reset record
    print("\n  Regenerating report...")
    if record:
        out = generate_html_report(record, report_path)
        log.info("Generated clean report: %s", out)
        print(f"  [OK] Report ready: {report_path.name}")
    else:
        print(f"  [SKIP] No record found to generate report")

    print("\n" + "=" * 55)
    print("  Batch data reset. Ready for fresh run.")
    print("=" * 55 + "\n")
    return 0


def _parse_iso_timestamp(value: str) -> datetime:
    """Parse ISO timestamp safely; returns datetime.min on failure."""
    if not value:
        return datetime.min
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return datetime.min


def _test_has_result_data(test: TestRun) -> bool:
    """Return True if a test has non-empty result data to merge."""
    return any([
        test.status in ("pass", "fail", "running"),
        bool(test.version_before),
        bool(test.version_after),
        bool(test.expected_version),
        bool(test.message),
        bool(test.started_at),
        bool(test.finished_at),
        bool(test.log_dir),
        bool(test.elapsed_seconds),
    ])


def _test_latest_timestamp(test: TestRun) -> datetime:
    """Return the newest known timestamp from a test result."""
    return max(
        _parse_iso_timestamp(test.finished_at),
        _parse_iso_timestamp(test.started_at),
    )


def _copy_result_fields(dst: TestRun, src: TestRun) -> None:
    """Copy result fields from src to dst, keeping id/extra_args unchanged."""
    dst.status = src.status
    dst.log_dir = src.log_dir
    dst.version_before = src.version_before
    dst.version_after = src.version_after
    dst.expected_version = src.expected_version
    dst.elapsed_seconds = src.elapsed_seconds
    dst.message = src.message
    dst.started_at = src.started_at
    dst.finished_at = src.finished_at
    dst.critical_failure = src.critical_failure


def cmd_merge(args: argparse.Namespace) -> int:
    """
    Merge result data from one or more record JSON files into current record.

    If both records have data for the same test id, keep the newer one based on
    finished_at/started_at timestamp.
    """
    if not args.merge_records:
        print("Error: --merge requires at least one record JSON path.")
        return 1

    batch_path, record_path = _selected_paths(args.local)
    report_path = _report_path_for(record_path)

    target = load_record(record_path)
    if target is None:
        base_args, tests = load_batch_config(batch_path)
        target = create_record(base_args, tests)

    target_by_id = {t.id: t for t in target.tests}
    merged = 0
    skipped = 0

    for src_path_raw in args.merge_records:
        src_path = Path(src_path_raw)
        src = load_record(src_path)
        if src is None:
            print(f"Warning: skipped invalid record file: {src_path}")
            continue

        for src_test in src.tests:
            dst_test = target_by_id.get(src_test.id)
            if dst_test is None:
                skipped += 1
                continue
            if not _test_has_result_data(src_test):
                continue

            if not _test_has_result_data(dst_test):
                _copy_result_fields(dst_test, src_test)
                merged += 1
                continue

            if _test_latest_timestamp(src_test) > _test_latest_timestamp(dst_test):
                _copy_result_fields(dst_test, src_test)
                merged += 1

    save_record(target, record_path)
    out = generate_html_report(target, report_path)
    print(
        f"Merged {merged} test result(s) into {record_path}. "
        f"Skipped {skipped} unknown test id(s)."
    )
    print(f"Report generated: {out}")
    return 0


# ── Helpers ───────────────────────────────────────────────────────────


def _load_monitor_state_safe() -> object:
    """Return monitor state if it exists, else None (never raises)."""
    try:
        from util_monitor import load_monitor_state
        return load_monitor_state()
    except Exception:
        return None


def _run_main_continue(result_file: Path) -> None:
    """
    Run ``main.py continue --result-file <path>`` and wait for it.

    Errors are logged as warnings so the batch runner can continue
    even if the continue command fails.
    """
    cmd = [
        sys.executable,
        str(Path(__file__).parent / "main.py"),
        "continue",
        "--result-file", str(result_file),
    ]
    log.info("Executing: main.py continue --result-file %s", result_file)
    try:
        subprocess.run(cmd, check=False)
    except Exception as exc:
        log.warning("main.py continue failed: %s", exc)


def _backup_record_file(record_path: Path) -> Optional[Path]:
    """Back up an existing record file to a fixed backup filename."""
    if not record_path.exists():
        return None

    if "local" in record_path.name:
        backup_path = record_path.parent / "batch_record_local_bk.json"
    else:
        backup_path = record_path.parent / "batch_record_bk.json"

    shutil.copy2(record_path, backup_path)
    log.info("Backed up record to %s", backup_path)
    print(f"Backed up to {backup_path.name}")
    return backup_path


def _backup_and_reset_record(batch_path: Path, record_path: Path) -> BatchRecord:
    """
    Back up an existing record file and leave a clean usable record behind.

    If the current record loads correctly, reset all tests to pending while
    keeping the same record structure. If the file is missing or corrupt,
    recreate it from the selected batch config.

    :return: The clean record that was saved to *record_path*.
    """
    record = load_record(record_path)
    if record_path.exists():
        _backup_record_file(record_path)

    if record is None:
        base_args, tests = load_batch_config(batch_path)
        record = create_record(base_args, tests)
        log.info("Created fresh record from %s", batch_path)
        print(f"Recreated clean record from {batch_path.name}")
    else:
        for test in record.tests:
            test.status = "pending"
            test.log_dir = ""
            test.version_before = ""
            test.version_after = ""
            test.expected_version = ""
            test.elapsed_seconds = 0.0
            test.message = ""
            test.started_at = ""
            test.finished_at = ""
            test.critical_failure = False

        record.started_at = ""
        record.finished_at = ""
        log.info("Reset record to pending — %s", record_path)
        print(f"Reset to pending — {record_path.name}")

    save_record(record, record_path)
    return record


# ── CLI ───────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="batch",
        description="Batch Netskope Client upgrade test runner",
    )
    parser.add_argument(
        "--local", action="store_true",
        help=(
            "Use local-MSI batch files: "
            "data/batch_local.json + log/batch_record_local.json"
        ),
    )
    parser.add_argument(
        "--merge", action="store_true",
        help=(
            "Merge result data from record JSON files into current record "
            "and regenerate HTML report"
        ),
    )
    parser.add_argument(
        "merge_records", nargs="*", metavar="RECORD_JSON",
        help="Record JSON files used by --merge",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume from an existing batch record, skipping completed tests",
    )
    parser.add_argument(
        "--continue", dest="do_continue", action="store_true",
        help="Resume after reboot (called by scheduled task)",
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Re-generate HTML report from existing record (no tests run)",
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help="Discard existing record and start a new batch from scratch",
    )
    parser.add_argument(
        "--retry-failed", dest="retry_failed", action="store_true",
        help="Reset all failed tests to pending and re-run them",
    )
    parser.add_argument(
        "--retry", nargs="+", metavar="ID",
        help="Reset specific test(s) by ID to pending and re-run",
    )
    parser.add_argument(
        "--retry-unknown", dest="retry_unknown", action="store_true",
        help="Reset failed tests with empty/unknown/N/A version_before to "
             "pending (failed before or during base MSI installation)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main() -> int:
    """Main entry point."""
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(verbose=args.verbose, file_logging=False)
    setup_batch_logging()

    if args.merge:
        return cmd_merge(args)

    if args.merge_records:
        print("Error: positional record files are only valid with --merge")
        return 1

    if args.report:
        return cmd_report(args)

    if args.do_continue:
        return cmd_continue(args)

    if args.fresh:
        return cmd_fresh(args)

    return cmd_run(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
        sys.exit(130)
