"""
Batch state management, HTML report generation, and scheduled-task
helpers for the Netskope Client batch upgrade runner.
"""

import json
import logging
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

BATCH_JSON = Path(__file__).parent / "data" / "batch.json"
BATCH_RECORD_JSON = Path(__file__).parent / "log" / "batch_record.json"
BATCH_TASK_NAME = "NsClientBatchContinue"
_MAIN_PY = Path(__file__).parent / "main.py"


# ── Data model ────────────────────────────────────────────────────────


@dataclass
class TestRun:
    """State and result of a single test in the batch."""

    id: str
    extra_args: str
    status: str = "pending"       # pending | running | pass | fail
    log_dir: str = ""
    version_before: str = ""
    version_after: str = ""
    expected_version: str = ""
    elapsed_seconds: float = 0.0
    message: str = ""
    started_at: str = ""
    finished_at: str = ""


@dataclass
class BatchRecord:
    """Full batch run state, persisted across runs and reboots."""

    batch_id: str
    base_args: str
    tests: list[TestRun] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""


# ── Config & record I/O ───────────────────────────────────────────────


def load_batch_config(path: Path = BATCH_JSON) -> tuple[str, list[dict]]:
    """
    Load batch.json and return (base_args, tests list).

    Each item in ``tests`` may be a plain string (treated as extra_args)
    or a dict with ``id`` and ``extra_args`` keys.  IDs are
    auto-generated as ``test_00``, ``test_01``, … for plain strings.

    :param path: Path to batch.json.
    :return: (base_args, list of {'id': str, 'extra_args': str}).
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    base_args: str = data.get("base_args", "")
    tests: list[dict] = []
    for i, item in enumerate(data.get("tests", [])):
        if isinstance(item, str):
            tests.append({"id": f"test_{i:02d}", "extra_args": item})
        else:
            tests.append({
                "id": item.get("id", f"test_{i:02d}"),
                "extra_args": item.get("extra_args", ""),
            })
    return base_args, tests


def create_record(base_args: str, tests: list[dict]) -> BatchRecord:
    """Create a fresh BatchRecord from loaded batch config data."""
    return BatchRecord(
        batch_id=datetime.now().strftime("%Y%m%d_%H%M%S"),
        base_args=base_args,
        tests=[TestRun(id=t["id"], extra_args=t["extra_args"]) for t in tests],
        started_at=datetime.now().isoformat(timespec="seconds"),
    )


def load_record(path: Path = BATCH_RECORD_JSON) -> Optional[BatchRecord]:
    """
    Load an existing batch record from JSON.

    :return: BatchRecord, or None if the file is missing or corrupt.
    """
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return BatchRecord(
            batch_id=data["batch_id"],
            base_args=data["base_args"],
            tests=[TestRun(**t) for t in data.get("tests", [])],
            started_at=data.get("started_at", ""),
            finished_at=data.get("finished_at", ""),
        )
    except Exception as exc:
        log.warning("Failed to load batch record from %s: %s", path, exc)
        return None


def save_record(record: BatchRecord, path: Path = BATCH_RECORD_JSON) -> None:
    """Persist the batch record to JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(record), f, indent=2)
        f.write("\n")
    log.debug("Batch record saved to %s", path)


# ── Result file I/O ───────────────────────────────────────────────────


def read_result_file(path: Path) -> Optional[dict]:
    """Read a JSON result file written by main.py, or None on failure."""
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        log.warning("Failed to read result file %s: %s", path, exc)
        return None


def apply_result_to_test(test: TestRun, result: dict) -> None:
    """Populate TestRun fields from a result dict written by main.py."""
    test.status = "pass" if result.get("success") else "fail"
    test.log_dir = result.get("log_dir", "")
    test.version_before = result.get("version_before", "")
    test.version_after = result.get("version_after", "")
    test.expected_version = result.get("expected_version", "")
    test.elapsed_seconds = float(result.get("elapsed_seconds", 0.0))
    test.message = result.get("message", "")[:200]
    if result.get("started_at"):
        test.started_at = result["started_at"]
    test.finished_at = (
        result["finished_at"]
        if result.get("finished_at")
        else datetime.now().isoformat(timespec="seconds")
    )


# ── Test execution ────────────────────────────────────────────────────


def has_reboot(extra_args: str) -> bool:
    """Return True if extra_args include --reboottime."""
    return "--reboottime" in extra_args


def run_test_subprocess(
    base_args: str,
    test: TestRun,
    result_file: Path,
    stop_event: threading.Event | None = None,
) -> None:
    """
    Execute a single test by invoking ``main.py`` as a subprocess.

    For non-reboot tests: blocks until the subprocess finishes and
    reads the result from *result_file*.  If *stop_event* is set while
    the subprocess is running, the subprocess is terminated and the test
    is marked as failed with message "Stopped by user".

    For reboot tests: the OS kills this process during the reboot,
    so this call does not return.  The batch continue task resumes
    execution after login.

    :param base_args: Base arg string (e.g. 'upgrade --target latest ...').
    :param test: TestRun to update in-place.
    :param result_file: Path where main.py writes the JSON result.
    :param stop_event: Optional event; when set, the subprocess is terminated.
    """
    if result_file.exists():
        result_file.unlink()

    test.status = "running"
    test.started_at = datetime.now().isoformat(timespec="seconds")

    args_parts = shlex.split(base_args, posix=False)
    if test.extra_args:
        args_parts += shlex.split(test.extra_args, posix=False)
    args_parts += ["--result-file", str(result_file)]

    cmd = [sys.executable, str(_MAIN_PY)] + args_parts
    log.info("Running [%s]: %s", test.id, " ".join(args_parts))

    try:
        proc = subprocess.Popen(cmd)
        while proc.poll() is None:
            if stop_event is not None and stop_event.is_set():
                log.warning("Stop requested — terminating subprocess for [%s]", test.id)
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                test.status = "fail"
                test.message = "Stopped by user"
                test.finished_at = datetime.now().isoformat(timespec="seconds")
                return
            time.sleep(0.5)
        result = read_result_file(result_file)
        if result:
            apply_result_to_test(test, result)
        else:
            test.status = "pass" if proc.returncode == 0 else "fail"
            test.finished_at = datetime.now().isoformat(timespec="seconds")
    except Exception as exc:
        log.warning("Subprocess error for [%s]: %s", test.id, exc)
        test.status = "fail"
        test.message = str(exc)[:200]
        test.finished_at = datetime.now().isoformat(timespec="seconds")


# ── Scheduled task helpers ────────────────────────────────────────────


def register_batch_continue_task() -> None:
    """
    Register a Windows scheduled task that calls
    ``batch.py --continue`` at next user logon.

    Uses ``/f`` to overwrite any existing task with the same name.
    """
    batch_py = Path(__file__).parent / "batch.py"
    cmd_str = f'"{sys.executable}" "{batch_py}" --continue'
    result = subprocess.run(
        [
            "schtasks", "/create",
            "/tn", BATCH_TASK_NAME,
            "/tr", cmd_str,
            "/sc", "ONLOGON",
            "/f",
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        log.info("Registered batch continue task: %s", BATCH_TASK_NAME)
    else:
        log.warning(
            "Failed to register batch continue task: %s",
            result.stderr.strip(),
        )


def delete_batch_continue_task() -> None:
    """Delete the batch continue scheduled task (silently if absent)."""
    subprocess.run(
        ["schtasks", "/delete", "/tn", BATCH_TASK_NAME, "/f"],
        capture_output=True, text=True, timeout=30,
    )
    log.info("Batch continue task removed (if it existed)")


# ── HTML report ───────────────────────────────────────────────────────


_STATUS_STYLE: dict[str, tuple[str, str]] = {
    "pass":    ("#16a34a", "#f0fdf4"),   # (text color, row background)
    "fail":    ("#dc2626", "#fef2f2"),
    "running": ("#ca8a04", "#fefce8"),
    "pending": ("#6b7280", "#f9fafb"),
}


def generate_html_report(
    record: BatchRecord,
    output_path: Optional[Path] = None,
) -> Path:
    """
    Generate a self-contained HTML upgrade batch report.

    :param record: BatchRecord to render.
    :param output_path: Target path; defaults to log/batch_report.html.
    :return: Path to the generated HTML file.
    """
    if output_path is None:
        output_path = BATCH_RECORD_JSON.parent / "batch_report.html"

    n_pass = sum(1 for t in record.tests if t.status == "pass")
    n_fail = sum(1 for t in record.tests if t.status == "fail")
    n_run  = sum(1 for t in record.tests if t.status == "running")
    n_pend = sum(1 for t in record.tests if t.status == "pending")
    total  = len(record.tests)

    rows_html = _build_table_rows(record.tests)
    gen_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html = (
        "<!DOCTYPE html>\n"
        "<html lang='en'>\n"
        "<head><meta charset='UTF-8'>\n"
        f"<title>Batch Report {record.batch_id}</title>\n"
        "<style>\n"
        "body{font-family:system-ui,sans-serif;margin:2rem;color:#1f2937;background:#f8fafc}\n"
        "h1{font-size:1.4rem;margin-bottom:.2rem}\n"
        ".meta{color:#6b7280;font-size:.85rem;margin-bottom:1.2rem}\n"
        ".base{background:#eff6ff;border-left:3px solid #3b82f6;padding:.5rem .75rem;\n"
        "       border-radius:.3rem;font-family:monospace;font-size:.85rem;margin-bottom:1.2rem}\n"
        ".summary{display:flex;gap:1rem;margin-bottom:1.5rem;flex-wrap:wrap}\n"
        ".badge{padding:.35rem .8rem;border-radius:.4rem;font-weight:600;font-size:.85rem}\n"
        "table{width:100%;border-collapse:collapse;font-size:.88rem;background:#fff;\n"
        "       border-radius:.5rem;overflow:hidden;box-shadow:0 1px 3px #0001}\n"
        "th{background:#1e293b;color:#f1f5f9;text-align:left;padding:.5rem .75rem;white-space:nowrap}\n"
        "td{padding:.4rem .75rem;border-bottom:1px solid #e2e8f0;vertical-align:top}\n"
        "tr:last-child td{border-bottom:none}\n"
        "tr:hover{filter:brightness(.97)}\n"
        "a{color:#2563eb}\n"
        ".mono{font-family:monospace;font-size:.82em}\n"
        ".msg{font-size:.8em;max-width:280px;word-break:break-word}\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        "<h1>Batch Upgrade Report</h1>\n"
        "<div class='meta'>\n"
        f"  Batch ID: <strong>{record.batch_id}</strong>&nbsp;|&nbsp;\n"
        f"  Started: {record.started_at or '&mdash;'}&nbsp;|&nbsp;"
        f"Finished: {record.finished_at or '&mdash;'}&nbsp;|&nbsp;\n"
        f"  Generated: {gen_time}\n"
        "</div>\n"
        f"<div class='base'>Base args:&nbsp; {record.base_args}</div>\n"
        "<div class='summary'>\n"
        f"  <span class='badge' style='background:#dcfce7;color:#16a34a'>PASS&nbsp;{n_pass}</span>\n"
        f"  <span class='badge' style='background:#fee2e2;color:#dc2626'>FAIL&nbsp;{n_fail}</span>\n"
        f"  <span class='badge' style='background:#fefce8;color:#ca8a04'>RUNNING&nbsp;{n_run}</span>\n"
        f"  <span class='badge' style='background:#f1f5f9;color:#64748b'>PENDING&nbsp;{n_pend}</span>\n"
        f"  <span class='badge' style='background:#e0f2fe;color:#0369a1'>TOTAL&nbsp;{total}</span>\n"
        "</div>\n"
        "<table><thead>\n"
        "<tr><th>ID</th><th>Extra Args</th><th>Status</th>"
        "<th>Before</th><th>After</th><th>Expected</th>"
        "<th>Started</th><th>Elapsed</th><th>Logs</th><th>Message</th></tr>\n"
        "</thead>\n"
        f"<tbody>\n{rows_html}\n</tbody></table>\n"
        "</body></html>\n"
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    log.info("HTML report written to %s", output_path)
    return output_path


def _build_table_rows(tests: list[TestRun]) -> str:
    rows = []
    for t in tests:
        txt, bg = _STATUS_STYLE.get(t.status, ("#6b7280", "#f9fafb"))
        elapsed = f"{t.elapsed_seconds:.0f}s" if t.elapsed_seconds else "&mdash;"
        extra = t.extra_args if t.extra_args else "<em style='color:#9ca3af'>(base only)</em>"
        msg = (t.message or "&mdash;").replace("<", "&lt;").replace(">", "&gt;")
        if t.log_dir:
            url = t.log_dir.replace("\\", "/")
            log_cell = f"<a href='file:///{url}'>open</a>"
        else:
            log_cell = "&mdash;"
        started = t.started_at[:16].replace("T", " ") if t.started_at else "&mdash;"
        rows.append(
            f"<tr style='background:{bg}'>"
            f"<td>{t.id}</td>"
            f"<td class='mono'>{extra}</td>"
            f"<td><span style='color:{txt};font-weight:700'>{t.status.upper()}</span></td>"
            f"<td>{t.version_before or '&mdash;'}</td>"
            f"<td>{t.version_after or '&mdash;'}</td>"
            f"<td>{t.expected_version or '&mdash;'}</td>"
            f"<td style='white-space:nowrap'>{started}</td>"
            f"<td>{elapsed}</td>"
            f"<td>{log_cell}</td>"
            f"<td class='msg'>{msg}</td>"
            f"</tr>"
        )
    return "\n".join(rows)
