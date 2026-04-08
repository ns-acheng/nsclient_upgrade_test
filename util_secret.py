"""
Encrypted password storage for the Netskope Client Upgrade Tool.
Saves an encrypted password file locally so the user is not prompted every run.
The encryption key and password file are both in data/ and MUST be git-ignored.
"""

import logging
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
KEY_FILE = DATA_DIR / ".secret.key"
PASSWORD_FILE = DATA_DIR / ".password.enc"


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


def save_password(password: str) -> Path:
    """
    Encrypt and save the password to disk.

    :param password: Plaintext password to encrypt.
    :return: Path to the encrypted password file.
    """
    key = _get_or_create_key()
    fernet = Fernet(key)
    encrypted = fernet.encrypt(password.encode("utf-8"))
    PASSWORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    PASSWORD_FILE.write_bytes(encrypted)
    log.info("Encrypted password saved to %s", PASSWORD_FILE)
    return PASSWORD_FILE


def load_password() -> str | None:
    """
    Load and decrypt the saved password.

    :return: Decrypted password string, or None if not available.
    """
    if not PASSWORD_FILE.exists() or not KEY_FILE.exists():
        return None
    try:
        key = KEY_FILE.read_bytes().strip()
        fernet = Fernet(key)
        encrypted = PASSWORD_FILE.read_bytes()
        return fernet.decrypt(encrypted).decode("utf-8")
    except (InvalidToken, Exception) as exc:
        log.warning("Failed to decrypt saved password: %s", exc)
        return None


def clear_password() -> None:
    """Remove the saved password and key files."""
    for f in (PASSWORD_FILE, KEY_FILE):
        if f.exists():
            f.unlink()
            log.info("Removed %s", f)
