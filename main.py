"""
CLI entry point for the Netskope Client Auto-Upgrade Tool.

First-time setup (saves tenant, username, and encrypted password):
    python main.py setup

Then just run (uses saved password automatically):
    python main.py versions
    python main.py upgrade --target latest
    python main.py upgrade --target golden --dot
    python main.py disable-upgrade
"""

import argparse
import getpass
import sys
import logging
from pathlib import Path

from util_config import load_config, save_config, validate_config, ToolConfig
from util_log import setup_logging
from util_secret import load_password, save_password, clear_password, cleanup_legacy_file
from util_webui import WebUIClient
from util_client import LocalClient
from upgrade_runner import UpgradeRunner, UpgradeResult, RebootVerifyResult

log = logging.getLogger(__name__)

MAX_LOGIN_ATTEMPTS = 3


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
        choices=["latest", "golden"],
        help="Upgrade target type",
    )
    upgrade_parser.add_argument(
        "--from-version", type=str, default=None,
        help="Build version for download fallback (e.g. 123.0.0)",
    )
    upgrade_parser.add_argument(
        "--dot", action="store_true",
        help="Enable dot release updates for golden upgrade",
    )
    upgrade_parser.add_argument(
        "--64bit", dest="is_64_bit", action="store_true",
        help="Use 64-bit client installer (Windows only)",
    )
    upgrade_parser.add_argument(
        "--email", type=str, default=None,
        help="Send email invite to this address before upgrade",
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
        "--64bit", dest="is_64_bit", action="store_true",
        help="Use 64-bit client installer (Windows only)",
    )
    disable_parser.add_argument(
        "--email", type=str, default=None,
        help="Send email invite to this address before upgrade",
    )

    # ── reboot-setup ────────────────────────────────────────────
    reboot_setup_parser = subparsers.add_parser(
        "reboot-setup",
        help="Phase 1: Enable upgrade and schedule a reboot to interrupt it",
    )
    reboot_setup_parser.add_argument(
        "--target", required=True,
        choices=["latest", "golden"],
        help="Upgrade target type",
    )
    reboot_setup_parser.add_argument(
        "--reboot-timing", required=True,
        help="Reboot timing: 'early', 'mid', 'late', or seconds (e.g. '45')",
    )
    reboot_setup_parser.add_argument(
        "--from-version", type=str, default=None,
        help="Build version for download fallback (e.g. 123.0.0)",
    )
    reboot_setup_parser.add_argument(
        "--64bit", dest="is_64_bit", action="store_true",
        help="Source install is 64-bit",
    )
    reboot_setup_parser.add_argument(
        "--target-64bit", dest="target_64_bit", action="store_true", default=None,
        help="Upgrade target is 64-bit (defaults to --64bit value)",
    )
    reboot_setup_parser.add_argument(
        "--dot", action="store_true",
        help="Enable dot release updates for golden upgrade",
    )
    reboot_setup_parser.add_argument(
        "--email", type=str, default=None,
        help="Send email invite to this address before install",
    )
    reboot_setup_parser.add_argument(
        "--stabilize-wait", type=int, default=300,
        help="Seconds to wait in verify phase after reboot (default: 300)",
    )

    # ── reboot-verify ───────────────────────────────────────────
    reboot_verify_parser = subparsers.add_parser(
        "reboot-verify",
        help="Phase 2: Verify client recovered after reboot-interrupt",
    )
    reboot_verify_parser.add_argument(
        "--stabilize-wait", type=int, default=None,
        help="Override seconds to wait for stabilization (default: from saved state)",
    )

    return parser


def connect_with_retry(webui: WebUIClient, cfg: ToolConfig) -> bool:
    """
    Try to connect to the tenant. On auth failure, clear the saved
    password and re-prompt up to MAX_LOGIN_ATTEMPTS total tries.

    :param webui: WebUIClient instance.
    :param cfg: ToolConfig (cfg.tenant.password is updated on retry).
    :return: True if connected, False if all attempts failed.
    """
    hostname = cfg.tenant.hostname
    username = cfg.tenant.username
    for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
        try:
            webui.connect(hostname, username, cfg.tenant.password)
            save_password(cfg.tenant.password, hostname, username)
            return True
        except Exception as exc:
            if "invalid username or password" not in str(exc).lower():
                raise
            remaining = MAX_LOGIN_ATTEMPTS - attempt
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
        if key in skip_keys:
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


def cmd_upgrade(cfg: ToolConfig, args: argparse.Namespace) -> int:
    """Run an upgrade scenario."""
    # Initialize local client (Phase 1 — no nsclient needed)
    client = LocalClient(platform=cfg.client.platform)

    # Connect to WebUI
    webui = WebUIClient()
    if not connect_with_retry(webui, cfg):
        return 1

    log.info("Client and WebUI initialized for upgrade scenario")

    # Create runner
    runner = UpgradeRunner(
        webui=webui,
        client=client,
        upgrade_cfg=cfg.upgrade,
        config_name=cfg.tenant.config_name,
        is_64_bit=args.is_64_bit,
    )

    # Execute scenario
    result: UpgradeResult

    if args.target == "latest":
        result = runner.run_upgrade_to_latest(
            from_version=args.from_version, invite_email=args.email,
        )

    elif args.target == "golden":
        result = runner.run_upgrade_to_golden(
            from_version=args.from_version, dot=args.dot, invite_email=args.email,
        )

    else:
        print(f"Error: Unknown target '{args.target}'")
        return 2

    # Print result summary
    _print_result(result)
    return 0 if result.success else 1


def cmd_disable_upgrade(cfg: ToolConfig, args: argparse.Namespace) -> int:
    """Verify auto-upgrade stays disabled (negative test)."""
    # Initialize local client (Phase 1 — no nsclient needed)
    client = LocalClient(platform=cfg.client.platform)

    webui = WebUIClient()
    if not connect_with_retry(webui, cfg):
        return 1

    runner = UpgradeRunner(
        webui=webui,
        client=client,
        upgrade_cfg=cfg.upgrade,
        config_name=cfg.tenant.config_name,
        is_64_bit=args.is_64_bit,
    )

    result = runner.run_upgrade_disabled(
        from_version=args.from_version, invite_email=args.email,
    )
    _print_result(result)
    return 0 if result.success else 1


def cmd_reboot_setup(cfg: ToolConfig, args: argparse.Namespace) -> int:
    """Phase 1: Enable upgrade and schedule reboot to interrupt it."""
    client = LocalClient(platform=cfg.client.platform)

    webui = WebUIClient()
    if not connect_with_retry(webui, cfg):
        return 1

    runner = UpgradeRunner(
        webui=webui,
        client=client,
        upgrade_cfg=cfg.upgrade,
        config_name=cfg.tenant.config_name,
        is_64_bit=args.is_64_bit,
    )

    target_64 = args.target_64_bit if args.target_64_bit is not None else args.is_64_bit

    result = runner.run_reboot_interrupt_setup(
        target_type=args.target,
        reboot_timing=args.reboot_timing,
        from_version=args.from_version,
        dot=args.dot,
        invite_email=args.email,
        target_64_bit=target_64,
        stabilize_wait=args.stabilize_wait,
    )
    _print_result(result)
    return 0 if result.success else 1


def cmd_reboot_verify(cfg: ToolConfig, args: argparse.Namespace) -> int:
    """Phase 2: Verify client recovered after reboot-interrupt."""
    client = LocalClient(platform=cfg.client.platform)

    # WebUI is optional for verify — version can come from nsclient
    webui = WebUIClient()
    try:
        if connect_with_retry(webui, cfg):
            log.info("WebUI connected for verify phase")
    except Exception:
        log.warning("WebUI connection failed — verify will use local checks only")

    runner = UpgradeRunner(
        webui=webui,
        client=client,
        upgrade_cfg=cfg.upgrade,
        config_name=cfg.tenant.config_name,
    )

    result = runner.run_reboot_verify(
        stabilize_wait=args.stabilize_wait,
    )
    _print_reboot_verify_result(result)
    return 0 if result.success else 1


def _print_reboot_verify_result(result: RebootVerifyResult) -> None:
    """Print a formatted reboot-verify result summary."""
    status_icon = "PASS" if result.success else "FAIL"
    print(f"\n{'=' * 60}")
    print(f"  [{status_icon}] {result.scenario}")
    print(f"{'=' * 60}")
    print(f"  Version before:       {result.version_before}")
    print(f"  Version after:        {result.version_after}")
    print(f"  Expected version:     {result.expected_version}")
    print(f"  Upgrade completed:    {result.upgrade_completed}")
    print(f"  Rolled back:          {result.rolled_back}")
    print(f"  Install dir valid:    {result.install_dir_valid}")
    print(f"  Watchdog binpath:     {result.watchdog_binpath}")
    print(f"  Watchdog path valid:  {result.watchdog_binpath_valid}")
    print(f"  Services:")
    for role, info in result.services.items():
        state_str = info.get("state", "N/A")
        exists_str = "yes" if info.get("exists") else "NO"
        print(f"    {role:10s} ({info.get('name', '?')}): exists={exists_str}, state={state_str}")
    print(f"  Elapsed:              {result.elapsed_seconds:.1f}s")
    print(f"  Message:              {result.message}")
    print()


def _print_result(result: UpgradeResult) -> None:
    """Print a formatted upgrade result summary."""
    status_icon = "PASS" if result.success else "FAIL"
    print(f"\n{'=' * 60}")
    print(f"  [{status_icon}] {result.scenario}")
    print(f"{'=' * 60}")
    print(f"  Version before:    {result.version_before}")
    print(f"  Version after:     {result.version_after}")
    print(f"  Expected version:  {result.expected_version}")
    print(f"  WebUI version:     {result.webui_version}")
    print(f"  Elapsed:           {result.elapsed_seconds:.1f}s")
    print(f"  Message:           {result.message}")
    print()


def _prompt_password(label: str) -> str:
    """Prompt for a password."""
    return getpass.getpass(f"  {label}: ")


def main() -> int:
    """Main entry point."""
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

    # Setup command doesn't need validation or password
    if args.command == "setup":
        setup_logging(verbose=args.verbose)
        return cmd_setup(cfg)

    # Auto-detect tenant and config name from local NSClient
    ns_info = LocalClient.detect_tenant_from_nsconfig()
    if ns_info:
        if not cfg.tenant.hostname:
            cfg.tenant.hostname = ns_info.tenant_hostname
            print(f"  Auto-detected tenant: {ns_info.tenant_hostname}")
        if not cfg.tenant.config_name and ns_info.config_name:
            cfg.tenant.config_name = ns_info.config_name
            print(f"  Auto-detected config: {ns_info.config_name}")

    # Resolve password BEFORE logging starts so the prompt is not buried
    #   1. CLI --password flag  (already in cfg)
    #   2. Saved encrypted password  (keyed by tenant + username)
    #   3. Prompt user
    require_tenant = args.command in (
        "versions", "upgrade", "disable-upgrade", "reboot-setup", "reboot-verify",
    )
    if require_tenant and cfg.tenant.hostname and cfg.tenant.username and not cfg.tenant.password:
        saved = load_password(cfg.tenant.hostname, cfg.tenant.username)
        if saved:
            cfg.tenant.password = saved
        else:
            cfg.tenant.password = _prompt_password(
                f"Password for {cfg.tenant.username}@{cfg.tenant.hostname}"
            )

    # Now start logging
    setup_logging(verbose=args.verbose)

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
        return cmd_upgrade(cfg, args)
    elif args.command == "disable-upgrade":
        return cmd_disable_upgrade(cfg, args)
    elif args.command == "reboot-setup":
        return cmd_reboot_setup(cfg, args)
    elif args.command == "reboot-verify":
        return cmd_reboot_verify(cfg, args)
    else:
        parser.print_help()
        return 2


if __name__ == "__main__":
    sys.exit(main())
