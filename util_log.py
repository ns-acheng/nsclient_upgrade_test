"""
Logging setup for the Netskope Client Upgrade Tool.
Configures file + console handlers with configurable verbosity.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).parent / "log"
LOG_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s - %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Third-party loggers to silence
NOISY_LOGGERS = ("urllib3", "requests", "chardet", "charset_normalizer", "selenium")

# webapi logs a noisy ERROR + traceback for CPCS auth fallback even though
# legacy login succeeds. Suppress entirely so it doesn't alarm users.
SILENT_LOGGERS = ("webapi.auth.authentication", "webapi.webapi")


def setup_logging(verbose: bool = False) -> logging.Logger:
    """
    Configure root logging with console and file handlers.

    :param verbose: If True, set level to DEBUG; otherwise INFO.
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

    # File handler — one log per run
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
    tool_logger.info("Logging initialized — level=%s, log_file=%s", logging.getLevelName(level), log_file)
    return tool_logger
