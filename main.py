"""
CLI entry point for the Netskope Client Auto-Upgrade Tool.

First-time setup (saves tenant + username to config, never saves password):
    python main.py setup

Then just run (password will be prompted):
    python main.py versions
    python main.py upgrade --target latest --from-version release-92.0.0
    python main.py upgrade --target golden --golden-index -1 --dot
"""

import argparse
import getpass
import sys
import logging
from pathlib import Path

from util_config import load_config, save_config, validate_config, ToolConfig
from util_log import setup_logging
from util_webui import WebUIClient
from util_client import LocalClient
from upgrade_runner import UpgradeRunner, UpgradeResult

log = logging.getLogger(__name__)


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
        choices=["latest", "golden", "disabled"],
        help="Upgrade target type",
    )
    upgrade_parser.add_argument(
        "--from-version", type=str, default=None,
        help="Build version to install before upgrade (e.g. release-92.0.0)",
    )
    upgrade_parser.add_argument(
        "--golden-index", type=int, default=-1,
        help="Golden version index: -1=latest, -2=N-1, -3=N-2 (default: -1)",
    )
    upgrade_parser.add_argument(
        "--dot", action="store_true",
        help="Enable dot release updates for golden upgrade",
    )

    return parser


def cmd_setup(cfg: ToolConfig) -> int:
    """Interactive setup — save tenant and username to config file."""
    print("\n=== Netskope Upgrade Tool Setup ===\n")
    print("  Tenant and username are saved to data/config.json.")
    print("  Password is NEVER saved — you will be prompted each run.\n")

    hostname = input(f"  Tenant hostname [{cfg.tenant.hostname or 'e.g. tenant.goskope.com'}]: ").strip()
    if hostname:
        cfg.tenant.hostname = hostname

    username = input(f"  Admin username  [{cfg.tenant.username or 'e.g. admin@netskope.com'}]: ").strip()
    if username:
        cfg.tenant.username = username

    platform = input(f"  Client platform [{cfg.client.platform}]: ").strip()
    if platform:
        cfg.client.platform = platform

    path = save_config(cfg)
    print(f"\n  Config saved to {path}")
    print("  Password will be prompted when you run versions/upgrade commands.\n")
    return 0


def cmd_versions(cfg: ToolConfig) -> int:
    """List available release versions from the tenant."""
    webui = WebUIClient()
    webui.connect(cfg.tenant.hostname, cfg.tenant.username, cfg.tenant.password)

    versions = webui.get_release_versions()

    print("\n=== Available Client Release Versions ===\n")
    print(f"  Latest version:    {versions.get('latestversion', 'N/A')}")

    golden = versions.get("goldenversions", [])
    print(f"  Golden versions:   {', '.join(sorted(golden)) if golden else 'N/A'}")

    print("\n  All major releases:")
    for key in sorted(versions):
        if key in ("goldenversions", "latestversion"):
            continue
        dot_releases = versions[key]
        if isinstance(dot_releases, list):
            print(f"    {key}: {', '.join(sorted(dot_releases))}")
        else:
            print(f"    {key}: {dot_releases}")

    print()
    return 0


def cmd_status(cfg: ToolConfig) -> int:
    """Show current local client status."""
    # Status doesn't require tenant connection — just local checks
    try:
        client = LocalClient()
        # Try direct nsclient operations without full initialization
        from nsclient.nsclient import get_nsclient_instance
        # Minimal init for local-only checks
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
    # Validate from-version for scenarios that need it
    if args.target in ("latest", "disabled") and not args.from_version:
        print("Error: --from-version is required for --target latest and --target disabled")
        return 2

    # Connect to WebUI
    webui = WebUIClient()
    webui.connect(cfg.tenant.hostname, cfg.tenant.username, cfg.tenant.password)

    # Initialize local client
    client = LocalClient()
    # Note: In production use, client.create() needs stack/tenant objects.
    # For now, the caller must ensure nsclient is properly configured.
    log.info("Client and WebUI initialized for upgrade scenario")

    # Create runner
    runner = UpgradeRunner(
        webui=webui,
        client=client,
        upgrade_cfg=cfg.upgrade,
    )

    # Execute scenario
    result: UpgradeResult

    if args.target == "latest":
        result = runner.run_upgrade_to_latest(from_version=args.from_version)

    elif args.target == "golden":
        result = runner.run_upgrade_to_golden(
            from_version=args.from_version,
            golden_index=args.golden_index,
            dot=args.dot,
        )

    elif args.target == "disabled":
        result = runner.run_upgrade_disabled(from_version=args.from_version)

    else:
        print(f"Error: Unknown target '{args.target}'")
        return 2

    # Print result summary
    _print_result(result)
    return 0 if result.success else 1


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


def main() -> int:
    """Main entry point."""
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 2

    # Setup logging
    setup_logging(verbose=args.verbose)

    # Load config
    cfg = load_config(
        config_path=args.config,
        tenant_hostname=args.tenant,
        tenant_username=args.username,
        tenant_password=args.password,
    )

    # Setup command doesn't need validation
    if args.command == "setup":
        return cmd_setup(cfg)

    # For commands that need a tenant, prompt for password if missing
    require_tenant = args.command in ("versions", "upgrade")
    if require_tenant and cfg.tenant.hostname and cfg.tenant.username and not cfg.tenant.password:
        cfg.tenant.password = getpass.getpass(
            f"  Password for {cfg.tenant.username}@{cfg.tenant.hostname}: "
        )

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
    else:
        parser.print_help()
        return 2


if __name__ == "__main__":
    sys.exit(main())
