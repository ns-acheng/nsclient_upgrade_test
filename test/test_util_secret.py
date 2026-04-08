"""
Unit tests for util_secret.py — multi-tenant password vault.
Uses a temp directory so no real secret files are touched.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import util_secret


# ── Constants for test data ────────────────────────────────────────────

HOST_A = "tenant-a.example.com"
USER_A = "admin@example.com"
HOST_B = "tenant-b.example.com"
USER_B = "ops@example.com"


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def tmp_data_dir(tmp_path: Path):
    """Redirect all secret file paths to a temp directory."""
    with patch.object(util_secret, "DATA_DIR", tmp_path), \
         patch.object(util_secret, "KEY_FILE", tmp_path / ".secret.key"), \
         patch.object(util_secret, "VAULT_FILE", tmp_path / ".passwords.enc"), \
         patch.object(util_secret, "_LEGACY_FILE", tmp_path / ".password.enc"):
        yield tmp_path


# ── Round-trip ───────────────────────────────────────────────────────


class TestSaveAndLoad:
    """Tests for save_password / load_password round-trip."""

    def test_save_then_load(self) -> None:
        """Saved password can be loaded back."""
        util_secret.save_password("my_secret_123", HOST_A, USER_A)
        assert util_secret.load_password(HOST_A, USER_A) == "my_secret_123"

    def test_overwrite_password(self) -> None:
        """Saving a new password replaces the old one."""
        util_secret.save_password("old_pass", HOST_A, USER_A)
        util_secret.save_password("new_pass", HOST_A, USER_A)
        assert util_secret.load_password(HOST_A, USER_A) == "new_pass"

    def test_empty_password(self) -> None:
        """Empty string can be saved and loaded."""
        util_secret.save_password("", HOST_A, USER_A)
        assert util_secret.load_password(HOST_A, USER_A) == ""

    def test_unicode_password(self) -> None:
        """Non-ASCII password survives round-trip."""
        util_secret.save_password("p@ss\u00e9\u00fc\u00f1", HOST_A, USER_A)
        assert util_secret.load_password(HOST_A, USER_A) == "p@ss\u00e9\u00fc\u00f1"


# ── Multi-tenant storage ────────────────────────────────────────────


class TestMultiTenant:
    """Tests for storing passwords for multiple tenants/users."""

    def test_different_tenants(self) -> None:
        """Passwords for different tenants are stored independently."""
        util_secret.save_password("pass_a", HOST_A, USER_A)
        util_secret.save_password("pass_b", HOST_B, USER_B)
        assert util_secret.load_password(HOST_A, USER_A) == "pass_a"
        assert util_secret.load_password(HOST_B, USER_B) == "pass_b"

    def test_same_tenant_different_users(self) -> None:
        """Different users on the same tenant have separate passwords."""
        util_secret.save_password("admin_pass", HOST_A, USER_A)
        util_secret.save_password("ops_pass", HOST_A, USER_B)
        assert util_secret.load_password(HOST_A, USER_A) == "admin_pass"
        assert util_secret.load_password(HOST_A, USER_B) == "ops_pass"

    def test_case_insensitive_lookup(self) -> None:
        """Hostname and username lookups are case-insensitive."""
        util_secret.save_password("secret", "Tenant.EXAMPLE.com", "Admin@Example.COM")
        assert util_secret.load_password("tenant.example.com", "admin@example.com") == "secret"

    def test_unknown_tenant_returns_none(self) -> None:
        """Loading a password for an unknown tenant returns None."""
        util_secret.save_password("pass_a", HOST_A, USER_A)
        assert util_secret.load_password(HOST_B, USER_B) is None


# ── Load without saved data ─────────────────────────────────────────


class TestLoadMissing:
    """Tests for load_password when files are absent or corrupt."""

    def test_no_files_returns_none(self) -> None:
        """Returns None when no password has been saved."""
        assert util_secret.load_password(HOST_A, USER_A) is None

    def test_missing_key_file(self, tmp_data_dir: Path) -> None:
        """Returns None if key file is missing but vault exists."""
        util_secret.save_password("test", HOST_A, USER_A)
        util_secret.KEY_FILE.unlink()
        assert util_secret.load_password(HOST_A, USER_A) is None

    def test_missing_vault_file(self, tmp_data_dir: Path) -> None:
        """Returns None if vault file is missing but key exists."""
        util_secret.save_password("test", HOST_A, USER_A)
        util_secret.VAULT_FILE.unlink()
        assert util_secret.load_password(HOST_A, USER_A) is None

    def test_corrupt_vault_file(self, tmp_data_dir: Path) -> None:
        """Returns None if vault file is corrupted."""
        util_secret.save_password("test", HOST_A, USER_A)
        util_secret.VAULT_FILE.write_bytes(b"not-valid-encrypted-data")
        assert util_secret.load_password(HOST_A, USER_A) is None

    def test_wrong_key(self, tmp_data_dir: Path) -> None:
        """Returns None if key doesn't match the encrypted vault."""
        from cryptography.fernet import Fernet
        util_secret.save_password("test", HOST_A, USER_A)
        util_secret.KEY_FILE.write_bytes(Fernet.generate_key())
        assert util_secret.load_password(HOST_A, USER_A) is None


# ── Clear ────────────────────────────────────────────────────────────


class TestClearPassword:
    """Tests for clear_password."""

    def test_clear_specific_entry(self, tmp_data_dir: Path) -> None:
        """Clears only the targeted tenant/user entry."""
        util_secret.save_password("pass_a", HOST_A, USER_A)
        util_secret.save_password("pass_b", HOST_B, USER_B)

        util_secret.clear_password(HOST_A, USER_A)

        assert util_secret.load_password(HOST_A, USER_A) is None
        assert util_secret.load_password(HOST_B, USER_B) == "pass_b"

    def test_clear_last_entry_removes_files(self, tmp_data_dir: Path) -> None:
        """Clearing the only entry removes the vault and key files."""
        util_secret.save_password("pass_a", HOST_A, USER_A)
        util_secret.clear_password(HOST_A, USER_A)

        assert not util_secret.VAULT_FILE.exists()
        assert not util_secret.KEY_FILE.exists()

    def test_clear_all_removes_files(self, tmp_data_dir: Path) -> None:
        """Clearing without args removes all files."""
        util_secret.save_password("pass_a", HOST_A, USER_A)
        assert util_secret.KEY_FILE.exists()
        assert util_secret.VAULT_FILE.exists()

        util_secret.clear_password()

        assert not util_secret.KEY_FILE.exists()
        assert not util_secret.VAULT_FILE.exists()

    def test_clear_when_no_files(self) -> None:
        """Does not raise when files don't exist."""
        util_secret.clear_password()

    def test_clear_specific_when_not_present(self) -> None:
        """Does not raise when targeted entry doesn't exist."""
        util_secret.save_password("pass_a", HOST_A, USER_A)
        util_secret.clear_password(HOST_B, USER_B)
        assert util_secret.load_password(HOST_A, USER_A) == "pass_a"


# ── Key generation ───────────────────────────────────────────────────


class TestKeyGeneration:
    """Tests for _get_or_create_key."""

    def test_key_created_on_first_save(self, tmp_data_dir: Path) -> None:
        """Key file is created on first save."""
        assert not util_secret.KEY_FILE.exists()
        util_secret.save_password("test", HOST_A, USER_A)
        assert util_secret.KEY_FILE.exists()

    def test_key_reused_on_second_save(self, tmp_data_dir: Path) -> None:
        """Same key is reused for subsequent saves."""
        util_secret.save_password("first", HOST_A, USER_A)
        key1 = util_secret.KEY_FILE.read_bytes()
        util_secret.save_password("second", HOST_A, USER_A)
        key2 = util_secret.KEY_FILE.read_bytes()
        assert key1 == key2


# ── Legacy cleanup ──────────────────────────────────────────────────


class TestLegacyCleanup:
    """Tests for cleanup_legacy_file."""

    def test_removes_legacy_file(self, tmp_data_dir: Path) -> None:
        """Removes the old single-password file when present."""
        util_secret._LEGACY_FILE.write_bytes(b"old-encrypted-data")
        util_secret.cleanup_legacy_file()
        assert not util_secret._LEGACY_FILE.exists()

    def test_no_error_when_missing(self) -> None:
        """Does not raise when legacy file doesn't exist."""
        util_secret.cleanup_legacy_file()
