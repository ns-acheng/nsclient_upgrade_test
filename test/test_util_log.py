"""
Unit tests for util_log.py — logging setup with folder-based output.
"""

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from util_log import (
    _shorten_version,
    build_log_dir_name,
    setup_folder_logging,
    setup_logging,
)


# ── _shorten_version ────────────────────────────────────────────────


class TestShortenVersion:
    """Tests for the version abbreviation helper."""

    def test_full_four_part(self) -> None:
        assert _shorten_version("132.0.0.1234") == "132.0"

    def test_three_part(self) -> None:
        assert _shorten_version("136.1.2") == "136.1"

    def test_two_part(self) -> None:
        assert _shorten_version("95.1") == "95.1"

    def test_single_part(self) -> None:
        assert _shorten_version("136") == "136"

    def test_strips_64bit_suffix(self) -> None:
        assert _shorten_version("136.1.2.3456 (64-bit)") == "136.1"

    def test_empty_string(self) -> None:
        assert _shorten_version("") == ""

    def test_disabled_keyword(self) -> None:
        assert _shorten_version("disabled") == "disabled"


# ── build_log_dir_name ──────────────────────────────────────────────


class TestBuildLogDirName:
    """Tests for the log folder name builder."""

    def test_basic_32bit(self) -> None:
        name = build_log_dir_name(
            from_version="132.0.0.1234",
            to_version="136.1.2.5678",
        )
        assert name.startswith("upgrade_")
        assert "_from132.0_to136.1" in name
        assert "x64" not in name
        assert "reboottime" not in name

    def test_64bit_suffix(self) -> None:
        name = build_log_dir_name(
            from_version="132.0.0.1234",
            to_version="136.1.2.5678",
            target_64_bit=True,
        )
        assert "_from132.0_to136.1x64" in name

    def test_reboot_time(self) -> None:
        name = build_log_dir_name(
            from_version="132.0.0.1234",
            to_version="136.1.2.5678",
            target_64_bit=True,
            reboot_time=3,
        )
        assert name.endswith("_reboottime_3")
        assert "x64_reboottime_3" in name

    def test_disabled_scenario(self) -> None:
        name = build_log_dir_name(
            from_version="132.0.0.1234",
            to_version="disabled",
        )
        assert "_from132.0_todisabled" in name

    def test_timestamp_format(self) -> None:
        """Name starts with upgrade_YYYYMMDD_HHMMSS_."""
        import re

        name = build_log_dir_name("1.0", "2.0")
        assert re.match(r"upgrade_\d{8}_\d{6}_", name)


# ── setup_logging ───────────────────────────────────────────────────


class TestSetupLogging:
    """Tests for the two-phase logging setup."""

    def test_file_logging_true_creates_file(self, tmp_path: Path) -> None:
        """Legacy mode creates a log file in LOG_DIR."""
        import util_log

        orig_dir = util_log.LOG_DIR
        util_log.LOG_DIR = tmp_path
        try:
            setup_logging(file_logging=True)
            log_files = list(tmp_path.glob("upgrade_*.log"))
            assert len(log_files) == 1
        finally:
            util_log.LOG_DIR = orig_dir
            logging.getLogger().handlers.clear()

    def test_file_logging_false_no_file(self, tmp_path: Path) -> None:
        """Console-only mode creates no log file."""
        import util_log

        orig_dir = util_log.LOG_DIR
        util_log.LOG_DIR = tmp_path
        try:
            setup_logging(file_logging=False)
            log_files = list(tmp_path.glob("upgrade_*.log"))
            assert len(log_files) == 0
        finally:
            util_log.LOG_DIR = orig_dir
            logging.getLogger().handlers.clear()


# ── setup_folder_logging ────────────────────────────────────────────


class TestSetupFolderLogging:
    """Tests for the folder-based file handler."""

    def test_creates_folder_and_file(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "test_scenario_folder"
        log_file = setup_folder_logging(log_dir)
        assert log_dir.is_dir()
        assert log_file.name == "upgrade.log"
        assert log_file.parent == log_dir
        # Clean up handler
        logging.getLogger().handlers = [
            h for h in logging.getLogger().handlers
            if not isinstance(h, logging.FileHandler)
        ]

    def test_custom_filename(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "custom"
        log_file = setup_folder_logging(
            log_dir, log_filename="upgrade_continue.log",
        )
        assert log_file.name == "upgrade_continue.log"
        logging.getLogger().handlers = [
            h for h in logging.getLogger().handlers
            if not isinstance(h, logging.FileHandler)
        ]

    def test_adds_file_handler_to_root(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "handler_check"
        logging.getLogger().handlers.clear()
        setup_folder_logging(log_dir)
        file_handlers = [
            h for h in logging.getLogger().handlers
            if isinstance(h, logging.FileHandler)
        ]
        assert len(file_handlers) == 1
        logging.getLogger().handlers.clear()
