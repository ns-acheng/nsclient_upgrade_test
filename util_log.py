"""
Logging setup for the Netskope Client Upgrade Tool.
Configures file + console handlers with configurable verbosity.

For upgrade scenarios, file logging starts immediately in a
timestamp-only folder.  Once the from/to versions are known the
folder is renamed via ``rename_log_dir()`` to include version info.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

LOG_DIR = Path(__file__).parent / "log"
LOG_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s - %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Third-party loggers to silence
NOISY_LOGGERS = ("urllib3", "requests", "chardet", "charset_normalizer", "selenium")

# webapi logs a noisy ERROR + traceback for CPCS auth fallback even though
# legacy login succeeds. Suppress entirely so it doesn't alarm users.
SILENT_LOGGERS = ("webapi.auth.authentication", "webapi.webapi")


def setup_logging(
    verbose: bool = False,
    file_logging: bool = True,
) -> logging.Logger:
    """
    Configure root logging with console (and optionally file) handlers.

    :param verbose: If True, set level to DEBUG; otherwise INFO.
    :param file_logging: If True, create a timestamped log file in
                         ``log/`` (legacy behaviour).  Set to False
                         when the caller will later call
                         ``setup_folder_logging()`` with a scenario-
                         specific directory.
    :return: The root logger for the tool.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    level = logging.DEBUG if verbose else logging.INFO

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove any pre-existing handlers to avoid duplicates
    root_logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    root_logger.addHandler(console_handler)

    log_file: Optional[Path] = None
    if file_logging:
        # File handler — one log per run (legacy flat-file mode)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = LOG_DIR / f"upgrade_{timestamp}.log"
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)  # Always capture debug in file
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        root_logger.addHandler(file_handler)

    # Quiet noisy third-party loggers
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in SILENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.CRITICAL)

    tool_logger = logging.getLogger("nsclient_upgrade")
    tool_logger.info(
        "Logging initialized — level=%s, log_file=%s",
        logging.getLevelName(level), log_file or "(console only)",
    )
    return tool_logger


# ── Folder-based logging (upgrade scenarios) ───────────────────────


def _shorten_version(version: str) -> str:
    """
    Shorten a full version string to major.minor.

    Example: '132.0.0.1234' -> '132.0',  '136.1.2 (64-bit)' -> '136.1'
    """
    clean = version.replace(" (64-bit)", "").strip()
    parts = clean.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else clean


def build_log_dir_name(
    from_version: str,
    to_version: str,
    target_64_bit: bool = False,
    reboot_time: Optional[int] = None,
) -> str:
    """
    Build a log folder name with timestamp and major scenario info.

    Examples::

        upgrade_20260409_091135_from132.0_to136.1x64
        upgrade_20260409_091135_from132.0_to136.1x64_reboottime_3

    :param from_version: Installed (source) version string.
    :param to_version: Target upgrade version string.
    :param target_64_bit: True when the upgrade target is 64-bit.
    :param reboot_time: Optional timing number that triggers a reboot.
    :return: Folder name string (no path prefix).
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    from_short = _shorten_version(from_version)
    to_short = _shorten_version(to_version)
    bitness = "x64" if target_64_bit else ""
    name = f"upgrade_{timestamp}_from{from_short}_to{to_short}{bitness}"
    if reboot_time is not None:
        name += f"_reboottime_{reboot_time}"
    return name


def setup_folder_logging(
    log_dir: Path,
    log_filename: str = "upgrade.log",
) -> Path:
    """
    Create a scenario log folder and add a file handler to it.

    Call this after ``setup_logging(file_logging=False)`` once the
    folder name is known (versions determined, etc.).

    :param log_dir: Full path to the log folder to create.
    :param log_filename: Name of the log file inside the folder.
    :return: Full path to the log file.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / log_filename

    root_logger = logging.getLogger()
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    root_logger.addHandler(file_handler)

    logging.getLogger("nsclient_upgrade").info(
        "File logging started — log_dir=%s", log_dir,
    )
    return log_file


def rename_log_dir(old_dir: Path, new_dir: Path) -> Path:
    """
    Rename a scenario log folder and update the file handler.

    Closes any file handler pointing into *old_dir*, renames the
    directory to *new_dir*, then re-attaches the handler at the new
    location (append mode so nothing is lost).

    :param old_dir: Current log folder path.
    :param new_dir: Desired new log folder path.
    :return: The new log folder path.
    """
    root_logger = logging.getLogger()

    # Find file handlers inside old_dir, close them, remember filenames
    log_filename: Optional[str] = None
    for handler in root_logger.handlers[:]:
        if isinstance(handler, logging.FileHandler):
            handler_path = Path(handler.baseFilename)
            if handler_path.parent == old_dir:
                log_filename = handler_path.name
                handler.close()
                root_logger.removeHandler(handler)

    old_dir.rename(new_dir)

    # Re-attach file handler in the renamed directory
    if log_filename:
        log_file = new_dir / log_filename
        file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        root_logger.addHandler(file_handler)

    logging.getLogger("nsclient_upgrade").info(
        "Log folder renamed — %s", new_dir,
    )
    return new_dir
