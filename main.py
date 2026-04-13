"""
CLI entry point for the Netskope Client Auto-Upgrade Tool.

First-time setup (saves tenant, username, and encrypted password):
    python main.py setup

Then just run (uses saved password automatically):
    python main.py versions
    python main.py upgrade --target latest
    python main.py upgrade --target golden
    python main.py upgrade --target golden-dot --email acheng@netskope.com
    python main.py disable-upgrade
"""

import argparse
import getpass
import logging
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from util_config import load_config, save_config, validate_config, ToolConfig
from util_log import LOG_DIR, setup_logging, setup_folder_logging
from util_secret import load_password, save_password, clear_password, cleanup_legacy_file
from util_webui import WebUIClient
from util_client import LocalClient, check_driver_install_log
from upgrade_runner import UpgradeRunner, UpgradeResult

log = logging.getLogger(__name__)

MAX_LOGIN_ATTEMPTS = 3
MAX_CONNECT_RETRIES = 5
CONNECT_RETRY_DELAY = 10  # seconds between connection retries

# Connection-level exceptions worth retrying (network/timeout issues)
_RETRIABLE_TYPES = (
    ConnectionError, TimeoutError, OSError,
)


def _is_connection_error(exc: BaseException) -> bool:
    """Return True if the exception is a retriable connection/network error."""
    exc_str = str(exc).lower()
    # Auth errors wrapped in TimeoutError (from login thread) are NOT
    # connection errors — they should go through the auth retry path.
    if "invalid username or password" in exc_str:
        return False
    if isinstance(exc, _RETRIABLE_TYPES):
        return True
    # requests wraps urllib3 errors as ConnectionError
    return any(hint in exc_str for hint in (
        "connection", "timed out", "timeout", "maxretryerror",
    ))


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="nsclient_upgrade",
        description="Netskope Client Auto-Upgrade Tool",
    )

    # Global options
    parser.add_argument(
        "--config", type=Path, default=None,
        help="Path to config JSON file (default: data/config.json)",
    )
    parser.add_argument(
        "--tenant", type=str, default=None,
        help="Tenant hostname (overrides config)",
    )
    parser.add_argument(
        "--username", type=str, default=None,
        help="Tenant admin username (overrides config)",
    )
    parser.add_argument(
        "--password", type=str, default=None,
        help="Tenant admin password (overrides config)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # ── setup ────────────────────────────────────────────────────
    subparsers.add_parser(
        "setup",
        help="Save tenant hostname and username to config (password is never saved)",
    )

    # ── versions ─────────────────────────────────────────────────
    subparsers.add_parser(
        "versions",
        help="List available client release versions from the tenant",
    )

    # ── status ───────────────────────────────────────────────────
    subparsers.add_parser(
        "status",
        help="Show current local client version and status",
    )

    # ── upgrade ──────────────────────────────────────────────────
    upgrade_parser = subparsers.add_parser(
        "upgrade",
        help="Run an auto-upgrade scenario",
    )
    upgrade_parser.add_argument(
        "--target", required=True,
        choices=["latest", "golden", "golden-dot", "local"],
        help=(
            "Upgrade target: latest, golden (base only), golden-dot "
            "(with dot release), or local (install from "
            "data/upgrade_version/stagent[64].msi)"
        ),
    )
    upgrade_parser.add_argument(
        "--from-version", type=str, default=None,
        help="Build version for download fallback (e.g. 123.0.0)",
    )
    upgrade_parser.add_argument(
        "--source-64bit", dest="source_64_bit", action="store_true",
        help="Source (base) install is 64-bit",
    )
    upgrade_parser.add_argument(
        "--target-64bit", dest="target_64_bit", action="store_true",
        help="Upgrade target is 64-bit",
    )
    upgrade_parser.add_argument(
        "--email", type=str, default=None,
        help="Send email invite to this address before upgrade",
    )
    upgrade_parser.add_argument(
        "--reboottime", type=int, default=None,
        choices=range(1, 14), metavar="N",
        help="Timing number (1-13) that triggers a reboot during upgrade",
    )
    upgrade_parser.add_argument(
        "--rebootdelay", type=int, default=5,
        help="Seconds to wait after timing fires before rebooting (default: 5)",
    )
    upgrade_parser.add_argument(
        "--action", type=int, default=None,
        choices=[2, 3, 4],
        help=(
            "Action to perform at reboot timing: "
            "2 = kill stAgentSvcMon then reboot, "
            "3 = kill stAgentSvcMon + msiexec then reboot, "
            "4 = kill stAgentSvcMon + msiexec + stAgentSvc then reboot"
        ),
    )
    upgrade_parser.add_argument(
        "--result-file", dest="result_file", default=None,
        help="Write JSON result to this path (used by batch runner)",
    )

    # ── disable-upgrade ─────────────────────────────────────────
    disable_parser = subparsers.add_parser(
        "disable-upgrade",
        help="Verify that auto-upgrade stays disabled (negative test)",
    )
    disable_parser.add_argument(
        "--from-version", type=str, default=None,
        help="Build version for download fallback (e.g. 123.0.0)",
    )
    disable_parser.add_argument(
        "--source-64bit", dest="source_64_bit", action="store_true",
        help="Source (base) install is 64-bit",
    )
    disable_parser.add_argument(
        "--email", type=str, default=None,
        help="Send email invite to this address before upgrade",
    )

    # ── continue ────────────────────────────────────────────────
    continue_parser = subparsers.add_parser(
        "continue",
        help="Resume timing monitor after reboot (auto-called by scheduled task)",
    )
    continue_parser.add_argument(
        "--timeout", type=int, default=180,
        help="Max seconds to wait for upgrade after reboot (default: 180)",
    )
    continue_parser.add_argument(
        "--result-file", dest="result_file", default=None,
        help="Write JSON result to this path (used by batch runner)",
    )

    return parser


def connect_with_retry(webui: WebUIClient, cfg: ToolConfig,
                       stop_event: threading.Event | None = None) -> bool:
    """
    Try to connect to the tenant with retry for both auth and connection errors.

    - Auth failures: re-prompt password up to MAX_LOGIN_ATTEMPTS times.
    - Connection errors (timeout, network): retry up to MAX_CONNECT_RETRIES
      times with a delay, then abort gracefully.
    - If *stop_event* is set (e.g. ESC pressed), abort immediately.

    :param webui: WebUIClient instance.
    :param cfg: ToolConfig (cfg.tenant.password is updated on retry).
    :param stop_event: Optional event checked between retries for early abort.
    :return: True if connected, False if all attempts failed or stopped.
    """
    hostname = cfg.tenant.hostname
    username = cfg.tenant.username
    auth_attempts = 0
    connect_failures = 0

    while auth_attempts < MAX_LOGIN_ATTEMPTS:
        if stop_event and stop_event.is_set():
            log.info("Stop requested — aborting connection retry")
            return False

        try:
            webui.connect(hostname, username, cfg.tenant.password)
            save_password(cfg.tenant.password, hostname, username)
            return True
        except Exception as exc:
            if _is_connection_error(exc):
                connect_failures += 1
                if connect_failures >= MAX_CONNECT_RETRIES:
                    log.error("Connection failed after %d attempts — aborting", MAX_CONNECT_RETRIES)
                    print(
                        f"\n  Connection to {hostname} failed after "
                        f"{MAX_CONNECT_RETRIES} attempts. Check network/VPN."
                    )
                    return False
                log.warning(
                    "Connection error (%d/%d): %s — retrying in %ds",
                    connect_failures, MAX_CONNECT_RETRIES,
                    exc, CONNECT_RETRY_DELAY,
                )
                print(
                    f"\n  Connection failed ({connect_failures}/{MAX_CONNECT_RETRIES})"
                    f" — retrying in {CONNECT_RETRY_DELAY}s..."
                )
                if stop_event:
                    stop_event.wait(CONNECT_RETRY_DELAY)
                    if stop_event.is_set():
                        log.info("Stop requested — aborting connection retry")
                        return False
                else:
                    time.sleep(CONNECT_RETRY_DELAY)
                continue

            if "invalid username or password" not in str(exc).lower():
                raise
            auth_attempts += 1
            remaining = MAX_LOGIN_ATTEMPTS - auth_attempts
            if remaining == 0:
                print(f"\n  Authentication failed after {MAX_LOGIN_ATTEMPTS} attempts.")
                return False
            print(f"\n  Login failed — invalid password. {remaining} attempt(s) remaining.")
            clear_password(hostname, username)
            cfg.tenant.password = _prompt_password(
                f"Password for {username}@{hostname}"
            )
    return False


def cmd_setup(cfg: ToolConfig) -> int:
    """Interactive setup — save tenant, username, and encrypted password."""
    print("\n=== Netskope Upgrade Tool Setup ===\n")
    print("  Tenant and username are saved to data/config.json.")
    print("  Password is encrypted and saved locally (never in git).\n")

    # Auto-detect tenant and config name from local NSClient installation
    ns_info = LocalClient.detect_tenant_from_nsconfig()
    if ns_info:
        if not cfg.tenant.hostname:
            print(f"  Detected tenant from NSClient: {ns_info.tenant_hostname}")
            cfg.tenant.hostname = ns_info.tenant_hostname
        if not cfg.tenant.config_name and ns_info.config_name:
            print(f"  Detected config name: {ns_info.config_name}")
            cfg.tenant.config_name = ns_info.config_name

    hostname = input(f"  Tenant hostname [{cfg.tenant.hostname or 'e.g. tenant.goskope.com'}]: ").strip()
    if hostname:
        cfg.tenant.hostname = hostname

    username = input(f"  Admin username  [{cfg.tenant.username or 'e.g. admin@netskope.com'}]: ").strip()
    if username:
        cfg.tenant.username = username

    platform = input(f"  Client platform [{cfg.client.platform}]: ").strip()
    if platform:
        cfg.client.platform = platform

    password = _prompt_password("Admin password").strip()
    if password and cfg.tenant.hostname and cfg.tenant.username:
        save_password(password, cfg.tenant.hostname, cfg.tenant.username)
        print("  Password encrypted and saved.")

    cleanup_legacy_file()
    path = save_config(cfg)
    print(f"\n  Config saved to {path}\n")
    return 0


def cmd_versions(cfg: ToolConfig) -> int:
    """List available release versions from the tenant."""
    webui = WebUIClient()
    if not connect_with_retry(webui, cfg):
        return 1

    versions = webui.get_release_versions()

    # Keys that are metadata, not version lists
    skip_keys = {"goldenversions", "latestversion", "versions_upload_timestamp"}

    print("\n=== Available Client Release Versions ===\n")
    print(f"  Latest version:    {versions.get('latestversion', 'N/A')}")

    golden = sorted(versions.get("goldenversions", []))
    if golden:
        latest_golden = golden[-1]
        golden_builds = versions.get(latest_golden, [])
        latest_golden_build = sorted(golden_builds)[-1] if golden_builds else "N/A"
        print(f"  Golden versions:   {', '.join(golden)}")
        print(f"  Latest golden build: {latest_golden_build}")
    else:
        print(f"  Golden versions:   N/A")

    print("\n  All major releases:")
    for key in sorted(versions):
        if not key[0:1].isdigit() or "." not in key:
            continue
        dot_releases = versions[key]
        if isinstance(dot_releases, list):
            print(f"    {key}: {', '.join(sorted(dot_releases))}")

    print()
    return 0


def _check_nsclient_available() -> bool:
    """
    Check if the nsclient package is importable.

    :return: True if available, False otherwise.
    """
    try:
        import nsclient  # noqa: F401
        return True
    except ModuleNotFoundError:
        return False


def cmd_status(cfg: ToolConfig) -> int:
    """Show current local client status."""
    if not _check_nsclient_available():
        print("\nError: nsclient package is not installed.")
        print("  Install the Netskope Client library to use this command.")
        return 1

    try:
        from nsclient.nsclient import get_nsclient_instance
        client_obj = get_nsclient_instance(
            platform=cfg.client.platform,
            email="status-check@local",
            password="",
            stack=None,
            tenant_name="",
        )
        version = client_obj.get_installed_version()
        status = client_obj.get_status()
        installed = client_obj.assert_installation()

        print("\n=== Netskope Client Status ===\n")
        print(f"  Installed:   {installed}")
        print(f"  Version:     {version}")
        print(f"  Status:      {status}")
        print()
        return 0

    except Exception as exc:
        log.error("Failed to check client status: %s", exc)
        print(f"\nError: Could not check client status — {exc}")
        print("  Is the Netskope Client installed on this machine?")
        return 1


def cmd_continue(args: argparse.Namespace) -> int:
    """Resume timing monitor after reboot, then run post-upgrade validation."""
    from util_monitor import (
        TimingMonitor, load_monitor_state, clear_monitor_state,
        delete_continue_task,
    )

    state = load_monitor_state()
    if state is None:
        print("Error: No monitor state file found. Nothing to continue.")
        return 1

    log_dir: Path | None = None
    if state.log_dir:
        log_dir = Path(state.log_dir)
        setup_folder_logging(log_dir, log_filename="upgrade_continue.log")
        log.info("Reusing pre-reboot log folder: %s", log_dir)

    # ── Early post-reboot check: UpgradeInProgress registry key ────
    # If the key is gone the upgrade already finished — skip the
    # monitor wait and go straight to post-upgrade validation.
    upgrade_still_running = _check_upgrade_in_progress(state)

    if upgrade_still_running:
        log.info("Resuming timing monitor from saved state")
        monitor = TimingMonitor(
            target_64_bit=state.target_64_bit,
            reboot_time=None,
            timeout=args.timeout,
            state=state,
        )
        monitor.start()
        completed = monitor.wait_for_upgrade_complete(
            timeout=args.timeout,
        )
        monitor.stop()
        monitor.print_report()
    else:
        log.info(
            "UpgradeInProgress key absent — upgrade already "
            "finished, skipping monitor wait"
        )
        completed = True

    clear_monitor_state()
    delete_continue_task()

    # Post-reboot validation: service, exe, registry, version comparison
    result = _run_post_reboot_validation(state, completed)
    _print_result(result)

    if not result.success:
        LocalClient.collect_log_bundle(
            state.target_64_bit or state.source_64_bit,
            log_dir or LOG_DIR,
        )

    if getattr(args, "result_file", None):
        _write_result_json(result, log_dir, args.result_file)
    elif state.original_argv:
        _try_record_manual_result(result, log_dir, state.original_argv)

    return 0 if result.success else 1


POSTURE_SETTLE_SECONDS = 30


def _wait_posture_settle(monitor: "TimingMonitor") -> None:
    """Wait until 30s after timing 12, then proceed with posture validation."""
    t12_offset = monitor.state.timings.get("12")
    if t12_offset is None:
        return
    monitor_start = datetime.fromisoformat(
        monitor.state.monitor_start_time
    ).timestamp()
    t12_abs = monitor_start + t12_offset
    remaining = POSTURE_SETTLE_SECONDS - (time.time() - t12_abs)
    if remaining > 0:
        log.info(
            "Waiting %.0fs for posture to settle after timing 12",
            remaining,
        )
        time.sleep(remaining)


def _run_post_reboot_validation(
    state: "MonitorState",
    completed: bool,
) -> "UpgradeResult":
    """
    Run local post-upgrade checks after a reboot-based upgrade.

    Does not require a WebUI connection — all checks are local.
    webui_version is left empty since no tenant credentials are available.

    :param state: Saved monitor state (carries version_before, expected_version, etc.).
    :param completed: Whether wait_for_upgrade_complete() returned True.
    :return: UpgradeResult with full validation details.
    """
    from util_client import LocalClient
    from upgrade_runner import UpgradeResult
    from util_verify import format_validation_issues, is_mismatch_only_failure

    # Version after: read from local exe, fall back to registry
    version_after = _get_local_version(state.target_64_bit)

    service_running = LocalClient.is_service_running()

    client = LocalClient(platform="windows")
    exe_validation = client.verify_executables(
        is_64_bit=state.target_64_bit,
        expected_version=version_after,
    )
    if state.source_64_bit != state.target_64_bit:
        stale = LocalClient.check_old_arch_cleanup(
            state.source_64_bit, state.target_64_bit,
        )
        exe_validation.stale_arch_files = stale
        if stale:
            exe_validation.valid = False

    uninstall_entry = client.check_uninstall_registry()
    validation_ok = exe_validation.valid and uninstall_entry.found

    version_ok = (
        not state.expected_version
        or version_after == state.expected_version
    )
    success = completed and service_running and version_ok and validation_ok

    if not completed:
        message = "Post-reboot monitor timed out — upgrade may not have finished"
    elif not version_ok:
        message = (
            f"Upgrade FAILED: expected {state.expected_version}, "
            f"got {version_after}"
        )
    elif success:
        message = (
            f"Upgrade successful: {state.version_before} -> {version_after}"
        )
    else:
        message = (
            f"Post-reboot checks failed: {state.version_before} -> {version_after}"
        )
    message += format_validation_issues(
        service_running, exe_validation, uninstall_entry,
    )
    driver_note = check_driver_install_log(exe_validation, service_running)
    if driver_note:
        message += driver_note
    log.info("Post-reboot validation: %s", message)

    return UpgradeResult(
        success=success,
        scenario=state.scenario or "continue",
        version_before=state.version_before,
        version_after=version_after,
        expected_version=state.expected_version,
        webui_version="",
        elapsed_seconds=0.0,
        message=message,
        service_running=service_running,
        exe_validation=exe_validation,
        uninstall_entry=uninstall_entry,
        critical_failure=(
            False
            if driver_note or is_mismatch_only_failure(
                exe_validation, service_running, uninstall_entry,
            )
            else not validation_ok
        ),
    )


def _check_upgrade_in_progress(state: "MonitorState") -> bool:
    """
    Early post-reboot check for ``HKLM\\SOFTWARE\\Netskope\\UpgradeInProgress``.

    :return: True if the upgrade is still in progress (key exists or
             service is still old with key present) — the caller should
             run the timing monitor.  False if the key is absent —
             upgrade is done, skip straight to validation.
    """
    from util_client import LocalClient

    key_exists = LocalClient.check_upgrade_in_progress()
    if key_exists:
        log.info("UpgradeInProgress key present — upgrade still running")
        return True

    current_version = _get_local_version(state.target_64_bit)
    log.info(
        "UpgradeInProgress key absent (version: %s → %s) — "
        "upgrade finished, proceeding to validation",
        state.version_before, current_version,
    )
    return False


def _get_local_version(target_64_bit: bool) -> str:
    """Read installed version from the local stAgentSvc.exe or registry."""
    from util_client import LocalClient
    for is_64, suffix in [(True, " (64-bit)"), (False, "")]:
        install_dir = LocalClient.get_install_dir(is_64)
        exe = install_dir / "stAgentSvc.exe"
        if exe.is_file():
            ver = LocalClient.get_file_version(exe)
            if ver:
                return f"{ver}{suffix}" if suffix else ver
    reg = LocalClient.check_uninstall_registry()
    return reg.display_version if reg.found else "unknown"


def cmd_upgrade(cfg: ToolConfig, args: argparse.Namespace,
                log_dir: Path | None = None) -> int:
    """Run an upgrade scenario."""
    from util_input import start_input_monitor

    start_time = datetime.now().isoformat(timespec="seconds")

    # ESC key monitor — sets stop_event for graceful shutdown
    stop_event = threading.Event()
    start_input_monitor(stop_event)

    # Initialize local client (Phase 1 — no nsclient needed)
    client = LocalClient(platform=cfg.client.platform)

    # Connect to WebUI
    webui = WebUIClient()
    if not connect_with_retry(webui, cfg, stop_event=stop_event):
        return 1

    log.info("Client and WebUI initialized for upgrade scenario")

    # Create runner
    runner = UpgradeRunner(
        webui=webui,
        client=client,
        upgrade_cfg=cfg.upgrade,
        config_name=cfg.tenant.config_name,
        source_64_bit=args.source_64_bit,
        target_64_bit=args.target_64_bit,
        reboot_time=args.reboottime,
        reboot_delay=args.rebootdelay,
        reboot_action=args.action,
        stop_event=stop_event,
        log_dir=log_dir,
        email_profiles=cfg.client.email_profiles,
        save_config_fn=lambda: save_config(cfg, args.config),
        batch_mode=bool(args.result_file),
        original_argv=sys.argv[1:],
    )

    # Execute scenario
    result: UpgradeResult

    if args.target == "latest":
        result = runner.run_upgrade_to_latest(
            from_version=args.from_version, invite_email=args.email,
        )

    elif args.target in ("golden", "golden-dot"):
        result = runner.run_upgrade_to_golden(
            from_version=args.from_version,
            dot=(args.target == "golden-dot"),
            invite_email=args.email,
        )

    elif args.target == "local":
        result = runner.run_upgrade_from_local(invite_email=args.email)

    else:
        print(f"Error: Unknown target '{args.target}'")
        return 2

    # Print result summary
    _print_result(result)
    if args.result_file:
        _write_result_json(result, runner.log_dir, args.result_file, started_at=start_time)
    _try_record_manual_result(result, runner.log_dir, sys.argv[1:], started_at=start_time)
    return 0 if result.success else 1


def cmd_disable_upgrade(cfg: ToolConfig, args: argparse.Namespace,
                        log_dir: Path | None = None) -> int:
    """Verify auto-upgrade stays disabled (negative test)."""
    from util_input import start_input_monitor

    # ESC key monitor — sets stop_event for graceful shutdown
    stop_event = threading.Event()
    start_input_monitor(stop_event)

    # Initialize local client (Phase 1 — no nsclient needed)
    client = LocalClient(platform=cfg.client.platform)

    webui = WebUIClient()
    if not connect_with_retry(webui, cfg, stop_event=stop_event):
        return 1

    runner = UpgradeRunner(
        webui=webui,
        client=client,
        upgrade_cfg=cfg.upgrade,
        config_name=cfg.tenant.config_name,
        source_64_bit=args.source_64_bit,
        stop_event=stop_event,
        log_dir=log_dir,
        email_profiles=cfg.client.email_profiles,
        save_config_fn=lambda: save_config(cfg, args.config),
    )

    result = runner.run_upgrade_disabled(
        from_version=args.from_version, invite_email=args.email,
    )
    _print_result(result)
    return 0 if result.success else 1


_GREEN = "\033[92m"
_RED = "\033[91m"
_RESET = "\033[0m"


def _icon(ok: bool) -> str:
    """Return a colored PASS/FAIL tag."""
    return f"{_GREEN}PASS{_RESET}" if ok else f"{_RED}FAIL{_RESET}"


def _print_result(result: UpgradeResult) -> None:
    """Print a formatted upgrade result summary to console and log file."""
    # Build plain-text report (no ANSI colors) for logging
    tag = "PASS" if result.success else "FAIL"
    lines: list[str] = []
    lines.append(f"{'=' * 60}")
    lines.append(f"  [{tag}] {result.scenario}")
    lines.append(f"{'=' * 60}")
    lines.append(f"  Version before:    {result.version_before}")
    lines.append(f"  Version after:     {result.version_after}")
    lines.append(f"  Expected version:  {result.expected_version}")
    lines.append(f"  WebUI version:     {result.webui_version}")
    svc_tag = "PASS" if result.service_running else "FAIL"
    lines.append(f"  Service running:   [{svc_tag}]")
    exe = result.exe_validation
    if exe:
        exe_tag = "PASS" if exe.valid else "FAIL"
        lines.append(f"  Executables [{exe_tag}]:  dir={exe.install_dir}")
        # Show required exes (exclude watchdog mon from this line)
        required_present = [e for e in exe.present if e != "stAgentSvcMon.exe"]
        required_missing = [e for e in exe.missing if e != "stAgentSvcMon.exe"]
        if required_present:
            lines.append(f"    Present:         {', '.join(required_present)}")
        if required_missing:
            lines.append(f"    MISSING:         {', '.join(required_missing)}")
        if exe.version_mismatches:
            for m in exe.version_mismatches:
                if "stAgentSvcMon.exe" not in m:
                    lines.append(f"    MISMATCH:        {m}")
        # Process running status for required exes
        req_running = [e for e in exe.processes_running if e != "stAgentSvcMon.exe"]
        req_not_running = [e for e in exe.processes_not_running if e != "stAgentSvcMon.exe"]
        if req_running:
            lines.append(f"    Running:         {', '.join(req_running)}")
        if req_not_running:
            lines.append(f"    Not running:     {', '.join(req_not_running)}")
        # Dedicated watchdog mon exe line
        if exe.watchdog_mode:
            mon_ver = next(
                (m.split(": ")[1].split(" ")[0]
                 for m in exe.version_mismatches if "stAgentSvcMon.exe" in m),
                None,
            )
            mon_running = "stAgentSvcMon.exe" in exe.processes_running
            mon_proc = "running" if mon_running else "NOT running"
            if "stAgentSvcMon.exe" in exe.missing:
                lines.append(f"    Watchdog mon:    [FAIL] stAgentSvcMon.exe MISSING")
            elif mon_ver:
                lines.append(f"    Watchdog mon:    [FAIL] stAgentSvcMon.exe version {mon_ver}, {mon_proc}")
            else:
                mon_path = Path(exe.install_dir) / "stAgentSvcMon.exe"
                ver = LocalClient.get_file_version(mon_path) if mon_path.is_file() else ""
                ver_str = f" (version {ver})" if ver else ""
                lines.append(f"    Watchdog mon:    [PASS] stAgentSvcMon.exe{ver_str}, {mon_proc}")
            # stwatchdog service check
            if exe.stwatchdog_running is not None:
                svc_tag = "PASS" if exe.stwatchdog_running else "FAIL"
                svc_state = "running" if exe.stwatchdog_running else "NOT running"
                lines.append(f"    stwatchdog svc:  [{svc_tag}] {svc_state}")
        else:
            lines.append(f"    Watchdog mon:    not in watchdog mode")
        if exe.stale_arch_files:
            lines.append(f"    Old arch files:  {', '.join(exe.stale_arch_files)}")
    unreg = result.uninstall_entry
    if unreg:
        unreg_tag = "PASS" if unreg.found else "FAIL"
        lines.append(f"  Uninstall entry [{unreg_tag}]:")
        if unreg.found:
            lines.append(f"    Name:            {unreg.display_name}")
            lines.append(f"    Version:         {unreg.display_version}")
            lines.append(f"    Location:        {unreg.install_location}")
        else:
            lines.append(f"    Not found in registry")
    lines.append(f"  Elapsed:           {result.elapsed_seconds:.1f}s")
    lines.append(f"  Message:           {result.message}")
    plain_report = "\n".join(lines)
    log.info("Final report:\n%s", plain_report)

    # Print colored version to console
    colored_lines: list[str] = []
    for line in lines:
        line = line.replace("[PASS]", f"[{_GREEN}PASS{_RESET}]")
        line = line.replace("[FAIL]", f"[{_RED}FAIL{_RESET}]")
        colored_lines.append(line)
    print("\n" + "\n".join(colored_lines) + "\n")


def _write_result_json(
    result: "UpgradeResult",
    log_dir: "Optional[Path]",
    path: str,
    started_at: str = "",
) -> None:
    """
    Write an UpgradeResult as JSON for the batch runner.

    :param result: The UpgradeResult to serialize.
    :param log_dir: The scenario log directory (may be None).
    :param path: File path to write.
    :param started_at: ISO timestamp when the test started.
    """
    import json as _json
    data = {
        "success": result.success,
        "critical_failure": result.critical_failure,
        "scenario": result.scenario,
        "version_before": result.version_before,
        "version_after": result.version_after,
        "expected_version": result.expected_version,
        "webui_version": result.webui_version,
        "elapsed_seconds": result.elapsed_seconds,
        "message": result.message,
        "log_dir": str(log_dir) if log_dir else "",
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(data, f, indent=2)
            f.write("\n")
        log.info("Result written to %s", path)
    except Exception as exc:
        log.warning("Failed to write result file: %s", exc)


# Flags to strip when matching manual argv against batch test args.
# These are meta-flags that don't affect which test is being run.
_STRIP_FLAGS_WITH_VAL = frozenset({
    "--config", "--tenant", "--username", "--password", "--result-file",
})
_STRIP_FLAGS_BOOL = frozenset({"-v", "--verbose"})


def _normalize_argv(tokens: list[str]) -> frozenset[str]:
    """
    Strip meta-flags from argv tokens and return a frozenset for
    order-insensitive comparison against batch test arg sets.
    """
    out: list[str] = []
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if tok in _STRIP_FLAGS_WITH_VAL:
            skip_next = True
            continue
        if tok in _STRIP_FLAGS_BOOL:
            continue
        out.append(tok)
    return frozenset(out)


def _try_record_manual_result(
    result: "UpgradeResult",
    log_dir: "Optional[Path]",
    argv: list[str],
    started_at: str = "",
) -> None:
    """
    If a batch record exists and *argv* matches a recordable test, update
    that test with the result.  Silently skips if no record or no match.

    For success results: updates any matching test.
    For failure/critical-failure results: updates a matching test only if
    the test is pending, or if the test is already failed but has no
    Before/After/Log information yet.

    Matching is order-insensitive: argv tokens are normalised to a
    frozenset and compared against each test's full args frozenset.

    :param result: UpgradeResult from this run.
    :param log_dir: Scenario log directory.
    :param argv: sys.argv[1:] from this invocation.
    :param started_at: ISO timestamp when the run started.
    """
    try:
        import shlex
        from util_batch import (
            BATCH_JSON, BATCH_RECORD_JSON,
            BATCH_LOCAL_JSON, BATCH_RECORD_LOCAL_JSON,
            apply_result_to_test, create_record, generate_html_report,
            load_batch_config, load_record, save_record,
        )

        # Route --target local runs to the dedicated local batch files.
        is_local_target = False
        try:
            tidx = argv.index("--target")
            if tidx + 1 < len(argv) and argv[tidx + 1] == "local":
                is_local_target = True
        except (ValueError, IndexError):
            pass

        batch_json = BATCH_LOCAL_JSON if is_local_target else BATCH_JSON
        batch_record_json = (
            BATCH_RECORD_LOCAL_JSON if is_local_target else BATCH_RECORD_JSON
        )
        report_html = batch_record_json.parent / (
            "batch_report_local.html" if is_local_target else "batch_report.html"
        )

        record = load_record(batch_record_json)
        if record is None:
            if not batch_json.exists():
                return
            base_args, tests = load_batch_config(batch_json)
            record = create_record(base_args, tests)
            log.info("Created batch record from %s", batch_json)

        manual_set = _normalize_argv(argv)
        is_failure = not result.success

        for test in record.tests:
            full = (record.base_args + " " + test.extra_args).strip()
            batch_set = _normalize_argv(shlex.split(full, posix=False))
            if batch_set != manual_set:
                continue

            # For failure results: only update if test is pending, or if
            # test is already failed with no Before/After/Log info.
            if is_failure:
                is_empty_fail = (
                    test.status == "fail"
                    and not test.version_before
                    and not test.version_after
                    and not test.log_dir
                )
                if test.status != "pending" and not is_empty_fail:
                    continue

            apply_result_to_test(test, {
                "success": result.success,
                "critical_failure": result.critical_failure,
                "log_dir": str(log_dir) if log_dir else "",
                "version_before": result.version_before,
                "version_after": result.version_after,
                "expected_version": result.expected_version,
                "elapsed_seconds": result.elapsed_seconds,
                "message": result.message,
                "started_at": started_at,
                "finished_at": datetime.now().isoformat(timespec="seconds"),
            })
            save_record(record, batch_record_json)
            generate_html_report(record, report_html)
            log.info("Manual result mapped to batch test [%s]", test.id)
            return
    except Exception as exc:
        log.debug("Could not map manual result to batch record: %s", exc)


def _prompt_password(label: str) -> str:
    """Prompt for a password."""
    return getpass.getpass(f"  {label}: ")


def _close_browsers_and_drivers() -> None:
    """Gracefully close browsers and kill stale chromedriver processes."""
    # Gracefully close browser windows (without killing the process tree)
    try:
        subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                'Get-Process -Name msedge,chrome,firefox'
                ' -ErrorAction SilentlyContinue'
                ' | Where-Object { $_.MainWindowHandle -ne 0 }'
                ' | ForEach-Object { $_.CloseMainWindow() | Out-Null }',
            ],
            capture_output=True, timeout=15,
        )
    except Exception:
        pass

    # Kill leftover chromedriver.exe from previous runs
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "chromedriver.exe"],
            capture_output=True,
        )
    except Exception:
        pass


def main() -> int:
    """Main entry point."""
    _close_browsers_and_drivers()
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 2

    # Load config quietly first (before logging) to resolve password early
    cfg = load_config(
        config_path=args.config,
        tenant_hostname=args.tenant,
        tenant_username=args.username,
        tenant_password=args.password,
    )

    # Setup and continue commands don't need tenant validation
    if args.command == "setup":
        setup_logging(verbose=args.verbose)
        log.info("main.py %s", " ".join(sys.argv[1:]))
        return cmd_setup(cfg)

    if args.command == "continue":
        setup_logging(verbose=args.verbose, file_logging=False)
        log.info("main.py %s", " ".join(sys.argv[1:]))
        return cmd_continue(args)

    # Auto-detect tenant from local NSClient (if installed).
    # config_name is NOT detected here for upgrade/disable-upgrade —
    # the runner resolves it after install + nsdiag -u sync, because
    # on a fresh machine nsconfig.json doesn't exist yet.
    ns_info = LocalClient.detect_tenant_from_nsconfig()
    if ns_info:
        if not cfg.tenant.hostname:
            cfg.tenant.hostname = ns_info.tenant_hostname
            print(f"  Auto-detected tenant: {ns_info.tenant_hostname}")
        skip_config_name = args.command in ("upgrade", "disable-upgrade")
        if not skip_config_name and not cfg.tenant.config_name and ns_info.config_name:
            cfg.tenant.config_name = ns_info.config_name
            print(f"  Auto-detected config: {ns_info.config_name}")

    # Resolve password BEFORE logging starts so the prompt is not buried
    #   1. CLI --password flag  (already in cfg)
    #   2. Saved encrypted password  (keyed by tenant + username)
    #   3. Prompt user
    require_tenant = args.command in (
        "versions", "upgrade", "disable-upgrade",
    )
    if require_tenant and cfg.tenant.hostname and cfg.tenant.username and not cfg.tenant.password:
        saved = load_password(cfg.tenant.hostname, cfg.tenant.username)
        if saved:
            cfg.tenant.password = saved
        else:
            cfg.tenant.password = _prompt_password(
                f"Password for {cfg.tenant.username}@{cfg.tenant.hostname}"
            )

    # Start logging — upgrade commands get a folder immediately so
    # early logs (WebUI connect, install, sync) are captured to file.
    is_upgrade_cmd = args.command in ("upgrade", "disable-upgrade")
    setup_logging(verbose=args.verbose, file_logging=not is_upgrade_cmd)
    log_dir: Path | None = None
    if is_upgrade_cmd:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_dir = LOG_DIR / f"upgrade_{timestamp}"
        setup_folder_logging(log_dir)
        # Clear any stale monitor state and scheduled task left from a
        # previous interrupted run so they don't fire unexpectedly on reboot.
        from util_monitor import (
            clear_monitor_state, delete_continue_task, MONITOR_STATE_PATH,
        )
        if MONITOR_STATE_PATH.is_file():
            log.warning(
                "Stale monitor_state.json found — removing before fresh start"
            )
            clear_monitor_state()
        delete_continue_task()
    log.info("main.py %s", " ".join(sys.argv[1:]))

    # Validate config
    errors = validate_config(cfg, require_tenant=require_tenant)
    if errors:
        for err in errors:
            print(f"Config error: {err}")
        return 2

    # Dispatch command
    if args.command == "versions":
        return cmd_versions(cfg)
    elif args.command == "status":
        return cmd_status(cfg)
    elif args.command == "upgrade":
        return cmd_upgrade(cfg, args, log_dir=log_dir)
    elif args.command == "disable-upgrade":
        return cmd_disable_upgrade(cfg, args, log_dir=log_dir)
    elif args.command == "continue":
        return cmd_continue(args)
    else:
        parser.print_help()
        return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  Stopped by user (ESC). Exiting.")
        sys.exit(130)
