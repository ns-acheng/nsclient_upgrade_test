"""
Encrypted password vault for the Netskope Client Upgrade Tool.

Stores encrypted passwords keyed by tenant hostname and username,
so the user is not prompted every run.  Supports multiple tenants
and accounts in a single vault file.

The encryption key and vault file are both in data/ and MUST be
git-ignored.
"""

import json
import logging
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
KEY_FILE = DATA_DIR / ".secret.key"
VAULT_FILE = DATA_DIR / ".passwords.enc"

# Legacy single-password file from before multi-tenant support
_LEGACY_FILE = DATA_DIR / ".password.enc"


def _get_or_create_key() -> bytes:
    """
    Load the Fernet key from disk, or generate a new one.

    :return: Fernet key bytes.
    """
    if KEY_FILE.exists():
        return KEY_FILE.read_bytes().strip()
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    KEY_FILE.write_bytes(key)
    log.info("Generated new encryption key at %s", KEY_FILE)
    return key


def _vault_key(hostname: str, username: str) -> str:
    """
    Build a vault lookup key from hostname and username.

    :param hostname: Tenant hostname.
    :param username: Username.
    :return: Normalized key string.
    """
    return f"{hostname.lower()}|{username.lower()}"


def _load_vault() -> dict[str, str]:
    """
    Decrypt and load the password vault from disk.

    :return: Dict mapping 'hostname|username' to plaintext password.
    """
    if not VAULT_FILE.exists() or not KEY_FILE.exists():
        return {}
    try:
        key = KEY_FILE.read_bytes().strip()
        fernet = Fernet(key)
        encrypted = VAULT_FILE.read_bytes()
        decrypted = fernet.decrypt(encrypted).decode("utf-8")
        return json.loads(decrypted)
    except (InvalidToken, json.JSONDecodeError, Exception) as exc:
        log.warning("Failed to decrypt password vault: %s", exc)
        return {}


def _save_vault(vault: dict[str, str]) -> None:
    """
    Encrypt and persist the password vault to disk.

    :param vault: Dict mapping 'hostname|username' to plaintext password.
    """
    key = _get_or_create_key()
    fernet = Fernet(key)
    plaintext = json.dumps(vault).encode("utf-8")
    encrypted = fernet.encrypt(plaintext)
    VAULT_FILE.parent.mkdir(parents=True, exist_ok=True)
    VAULT_FILE.write_bytes(encrypted)
    log.info("Password vault saved to %s", VAULT_FILE)


def save_password(password: str, hostname: str, username: str) -> Path:
    """
    Encrypt and save a password for a specific tenant/user.

    :param password: Plaintext password to encrypt.
    :param hostname: Tenant hostname.
    :param username: Username for the tenant.
    :return: Path to the encrypted vault file.
    """
    vault = _load_vault()
    vault[_vault_key(hostname, username)] = password
    _save_vault(vault)
    log.info("Password saved for %s@%s", username, hostname)
    return VAULT_FILE


def load_password(hostname: str, username: str) -> str | None:
    """
    Load and decrypt the saved password for a specific tenant/user.

    :param hostname: Tenant hostname.
    :param username: Username for the tenant.
    :return: Decrypted password string, or None if not available.
    """
    vault = _load_vault()
    return vault.get(_vault_key(hostname, username))


def clear_password(hostname: str | None = None, username: str | None = None) -> None:
    """
    Remove saved password(s).

    If hostname and username are given, removes only that entry.
    Otherwise removes the entire vault and key files.

    :param hostname: Tenant hostname (optional).
    :param username: Username (optional).
    """
    if hostname and username:
        vault = _load_vault()
        key = _vault_key(hostname, username)
        if key in vault:
            del vault[key]
            if vault:
                _save_vault(vault)
            else:
                for f in (VAULT_FILE, KEY_FILE):
                    if f.exists():
                        f.unlink()
                        log.info("Removed %s", f)
            log.info("Cleared password for %s@%s", username, hostname)
    else:
        for f in (VAULT_FILE, KEY_FILE):
            if f.exists():
                f.unlink()
                log.info("Removed %s", f)


def cleanup_legacy_file() -> None:
    """Remove the legacy single-password file if it exists."""
    if _LEGACY_FILE.exists():
        _LEGACY_FILE.unlink()
        log.info("Removed legacy password file %s", _LEGACY_FILE)
