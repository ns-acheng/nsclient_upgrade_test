"""
Configuration loader for the Netskope Client Upgrade Tool.
Reads data/config.json and allows CLI argument overrides.
Password is never saved to disk — prompted at runtime when needed.
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).parent / "data" / "config.json"
REBOOT_STATE_PATH = Path(__file__).parent / "data" / "reboot_state.json"

# Fields that must never be written to the config file
SENSITIVE_FIELDS = {"password"}


@dataclass
class TenantConfig:
    """Tenant connection settings."""
    hostname: str = ""
    username: str = ""
    password: str = ""
    config_name: str = ""


@dataclass
class ClientConfig:
    """Local client settings."""
    platform: str = "windows"
    email_suffix: str = ""


@dataclass
class UpgradeConfig:
    """Upgrade timing and polling settings."""
    poll_interval_seconds: int = 30
    max_wait_seconds: int = 360
    config_update_wait_seconds: int = 15


@dataclass
class ToolConfig:
    """Top-level configuration container."""
    tenant: TenantConfig = field(default_factory=TenantConfig)
    client: ClientConfig = field(default_factory=ClientConfig)
    upgrade: UpgradeConfig = field(default_factory=UpgradeConfig)


@dataclass
class RebootTestState:
    """Persisted state for the two-phase reboot-interrupt test."""
    scenario: str           # "reboot_interrupt"
    version_before: str     # e.g. "135.0.0.2631"
    target_type: str        # "latest" or "golden"
    expected_version: str   # e.g. "136.0.4.2612"
    reboot_timing: str      # "early" / "mid" / "late" / "<N>"
    source_64_bit: bool     # was the base install 64-bit?
    target_64_bit: bool     # is the upgrade target 64-bit?
    config_name: str        # tenant config name
    stabilize_wait: int     # seconds to wait after reboot before checks
    timestamp: str          # ISO timestamp when setup ran


def save_reboot_state(
    state: RebootTestState,
    path: Optional[Path] = None,
) -> Path:
    """
    Write reboot test state to JSON for the verify phase.

    :param state: RebootTestState to persist.
    :param path: Override file path (for testing).
    :return: Path the state was saved to.
    """
    target = path or REBOOT_STATE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(asdict(state), f, indent=4)
        f.write("\n")
    log.info("Reboot state saved to %s", target)
    return target


def load_reboot_state(
    path: Optional[Path] = None,
) -> Optional[RebootTestState]:
    """
    Load reboot test state from JSON.

    :param path: Override file path (for testing).
    :return: RebootTestState or None if file missing / invalid.
    """
    target = path or REBOOT_STATE_PATH
    if not target.is_file():
        log.warning("No reboot state file at %s", target)
        return None
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        return RebootTestState(**data)
    except Exception as exc:
        log.warning("Failed to load reboot state: %s", exc)
        return None


def clear_reboot_state(path: Optional[Path] = None) -> None:
    """
    Delete the reboot state file after verify completes.

    :param path: Override file path (for testing).
    """
    target = path or REBOOT_STATE_PATH
    if target.is_file():
        target.unlink()
        log.info("Reboot state cleared: %s", target)


def load_config(
    config_path: Optional[Path] = None,
    tenant_hostname: Optional[str] = None,
    tenant_username: Optional[str] = None,
    tenant_password: Optional[str] = None,
) -> ToolConfig:
    """
    Load configuration from JSON file, then apply CLI overrides.

    :param config_path: Path to config JSON. Defaults to data/config.json.
    :param tenant_hostname: CLI override for tenant hostname.
    :param tenant_username: CLI override for tenant username.
    :param tenant_password: CLI override for tenant password.
    :return: Populated ToolConfig instance.
    """
    path = config_path or DEFAULT_CONFIG_PATH

    if path.exists():
        log.info("Loading config from %s", path)
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    else:
        log.warning("Config file not found at %s — using defaults", path)
        raw = {}

    # Build config from file
    tenant_raw = raw.get("tenant", {})
    client_raw = raw.get("client", {})
    upgrade_raw = raw.get("upgrade", {})

    cfg = ToolConfig(
        tenant=TenantConfig(
            hostname=tenant_raw.get("hostname", ""),
            username=tenant_raw.get("username", ""),
            password=tenant_raw.get("password", ""),
            config_name=tenant_raw.get("config_name", ""),
        ),
        client=ClientConfig(
            platform=client_raw.get("platform", "windows"),
            email_suffix=client_raw.get("email_suffix", ""),
        ),
        upgrade=UpgradeConfig(
            poll_interval_seconds=upgrade_raw.get("poll_interval_seconds", 30),
            max_wait_seconds=upgrade_raw.get("max_wait_seconds", 360),
            config_update_wait_seconds=upgrade_raw.get("config_update_wait_seconds", 15),
        ),
    )

    # Apply CLI overrides
    if tenant_hostname:
        cfg.tenant.hostname = tenant_hostname
    if tenant_username:
        cfg.tenant.username = tenant_username
    if tenant_password:
        cfg.tenant.password = tenant_password

    return cfg


def validate_config(cfg: ToolConfig, require_tenant: bool = True) -> list[str]:
    """
    Validate that required configuration fields are populated.

    :param cfg: The ToolConfig to validate.
    :param require_tenant: Whether tenant fields are required.
    :return: List of validation error messages (empty if valid).
    """
    errors: list[str] = []

    if require_tenant:
        if not cfg.tenant.hostname:
            errors.append("tenant.hostname is required (use --tenant or run 'setup')")
        if not cfg.tenant.username:
            errors.append("tenant.username is required (use --username or run 'setup')")
        if not cfg.tenant.password:
            errors.append("tenant.password is required (use --password or it will be prompted)")

    if cfg.upgrade.poll_interval_seconds <= 0:
        errors.append("upgrade.poll_interval_seconds must be positive")
    if cfg.upgrade.max_wait_seconds <= 0:
        errors.append("upgrade.max_wait_seconds must be positive")

    return errors


def _strip_sensitive(data: dict) -> dict:
    """
    Recursively remove sensitive fields from a dict before writing to disk.

    :param data: Dict to sanitize.
    :return: New dict with sensitive keys removed entirely.
    """
    clean: dict = {}
    for key, value in data.items():
        if key in SENSITIVE_FIELDS:
            continue
        elif isinstance(value, dict):
            clean[key] = _strip_sensitive(value)
        else:
            clean[key] = value
    return clean


def save_config(cfg: ToolConfig, config_path: Optional[Path] = None) -> Path:
    """
    Save configuration to JSON file. Passwords are never written.

    :param cfg: The ToolConfig to save.
    :param config_path: Target path. Defaults to data/config.json.
    :return: Path the config was saved to.
    """
    path = config_path or DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    data = asdict(cfg)
    safe_data = _strip_sensitive(data)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(safe_data, f, indent=4)
        f.write("\n")

    log.info("Config saved to %s (passwords excluded)", path)
    return path
