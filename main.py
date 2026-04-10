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
from util_client import LocalClient
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
        choices=["latest", "golden", "golden-dot"],
        help="Upgrade target: latest, golden (base only), or golden-dot (with dot release)",
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
        choices=[2, 3],
        help=(
            "Action to perform at reboot timing: "
            "2 = kill stAgentSvcMon then reboot, "
            "3 = kill stAgentSvcMon + msiexec then reboot"
        ),
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
        "--timeout", type=int, default=600,
        help="Max seconds to wait for remaining timings (default: 600)",
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
    """Resume timing monitor after reboot."""
    from util_monitor import (
        TimingMonitor, load_monitor_state, clear_monitor_state,
        delete_continue_task,
    )

    state = load_monitor_state()
    if state is None:
        print("Error: No monitor state file found. Nothing to continue.")
        return 1

    # Reuse the same log folder from before reboot (feature b)
    if state.log_dir:
        log_dir = Path(state.log_dir)
        setup_folder_logging(log_dir, log_filename="upgrade_continue.log")
        log.info("Reusing pre-reboot log folder: %s", log_dir)

    log.info("Resuming timing monitor from saved state")
    monitor = TimingMonitor(
        target_64_bit=state.target_64_bit,
        reboot_time=None,
        timeout=args.timeout,
        state=state,
    )
    monitor.start()
    monitor.wait_for_upgrade_complete(timeout=args.timeout)
    monitor.stop()
    monitor.print_report()

    clear_monitor_state()
    delete_continue_task()

    return 0


def cmd_upgrade(cfg: ToolConfig, args: argparse.Namespace,
                log_dir: Path | None = None) -> int:
    """Run an upgrade scenario."""
    from util_input import start_input_monitor

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

    else:
        print(f"Error: Unknown target '{args.target}'")
        return 2

    # Print result summary
    _print_result(result)
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
        if exe.present:
            lines.append(f"    Present:         {', '.join(exe.present)}")
        if exe.missing:
            lines.append(f"    MISSING:         {', '.join(exe.missing)}")
        if exe.version_mismatches:
            for m in exe.version_mismatches:
                lines.append(f"    MISMATCH:        {m}")
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
        return cmd_setup(cfg)

    if args.command == "continue":
        setup_logging(verbose=args.verbose, file_logging=False)
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
