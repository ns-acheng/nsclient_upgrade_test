"""
Unit tests for util_secret.py.
Uses a temp directory so no real secret files are touched.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import util_secret


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def tmp_data_dir(tmp_path: Path):
    """Redirect all secret file paths to a temp directory."""
    with patch.object(util_secret, "DATA_DIR", tmp_path), \
         patch.object(util_secret, "KEY_FILE", tmp_path / ".secret.key"), \
         patch.object(util_secret, "PASSWORD_FILE", tmp_path / ".password.enc"):
        yield tmp_path


# ── Round-trip ───────────────────────────────────────────────────────


class TestSaveAndLoad:
    """Tests for save_password / load_password round-trip."""

    def test_save_then_load(self) -> None:
        """Saved password can be loaded back."""
        util_secret.save_password("my_secret_123")
        assert util_secret.load_password() == "my_secret_123"

    def test_overwrite_password(self) -> None:
        """Saving a new password replaces the old one."""
        util_secret.save_password("old_pass")
        util_secret.save_password("new_pass")
        assert util_secret.load_password() == "new_pass"

    def test_empty_password(self) -> None:
        """Empty string can be saved and loaded."""
        util_secret.save_password("")
        assert util_secret.load_password() == ""

    def test_unicode_password(self) -> None:
        """Non-ASCII password survives round-trip."""
        util_secret.save_password("p@ss\u00e9\u00fc\u00f1")
        assert util_secret.load_password() == "p@ss\u00e9\u00fc\u00f1"


# ── Load without saved data ─────────────────────────────────────────


class TestLoadMissing:
    """Tests for load_password when files are absent or corrupt."""

    def test_no_files_returns_none(self) -> None:
        """Returns None when no password has been saved."""
        assert util_secret.load_password() is None

    def test_missing_key_file(self, tmp_data_dir: Path) -> None:
        """Returns None if key file is missing but password file exists."""
        util_secret.save_password("test")
        util_secret.KEY_FILE.unlink()
        assert util_secret.load_password() is None

    def test_missing_password_file(self, tmp_data_dir: Path) -> None:
        """Returns None if password file is missing but key exists."""
        util_secret.save_password("test")
        util_secret.PASSWORD_FILE.unlink()
        assert util_secret.load_password() is None

    def test_corrupt_password_file(self, tmp_data_dir: Path) -> None:
        """Returns None if password file is corrupted."""
        util_secret.save_password("test")
        util_secret.PASSWORD_FILE.write_bytes(b"not-valid-encrypted-data")
        assert util_secret.load_password() is None

    def test_wrong_key(self, tmp_data_dir: Path) -> None:
        """Returns None if key doesn't match the encrypted password."""
        from cryptography.fernet import Fernet
        util_secret.save_password("test")
        # Overwrite with a different key
        util_secret.KEY_FILE.write_bytes(Fernet.generate_key())
        assert util_secret.load_password() is None


# ── Clear ────────────────────────────────────────────────────────────


class TestClearPassword:
    """Tests for clear_password."""

    def test_clear_removes_files(self, tmp_data_dir: Path) -> None:
        """Both key and password files are deleted."""
        util_secret.save_password("test")
        assert util_secret.KEY_FILE.exists()
        assert util_secret.PASSWORD_FILE.exists()

        util_secret.clear_password()

        assert not util_secret.KEY_FILE.exists()
        assert not util_secret.PASSWORD_FILE.exists()

    def test_clear_when_no_files(self) -> None:
        """Does not raise when files don't exist."""
        util_secret.clear_password()  # Should not raise


# ── Key generation ───────────────────────────────────────────────────


class TestKeyGeneration:
    """Tests for _get_or_create_key."""

    def test_key_created_on_first_save(self, tmp_data_dir: Path) -> None:
        """Key file is created on first save."""
        assert not util_secret.KEY_FILE.exists()
        util_secret.save_password("test")
        assert util_secret.KEY_FILE.exists()

    def test_key_reused_on_second_save(self, tmp_data_dir: Path) -> None:
        """Same key is reused for subsequent saves."""
        util_secret.save_password("first")
        key1 = util_secret.KEY_FILE.read_bytes()
        util_secret.save_password("second")
        key2 = util_secret.KEY_FILE.read_bytes()
        assert key1 == key2
