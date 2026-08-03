# logger.py
"""
Central logging configuration for the Empire system.
Provides both console output and rotating file logs.
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from rich.logging import RichHandler

_LOG_DIR = "logs"
os.makedirs(_LOG_DIR, exist_ok=True)

def setup_logging(level=logging.INFO):
    """Initialize the root logger with console and file handlers."""
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # If handlers already exist, don't add them again (e.g., on reload)
    if root_logger.handlers:
        return

    # 1. Console handler using Rich (for pretty terminal output)
    console_handler = RichHandler(
        rich_tracebacks=True,
        tracebacks_show_locals=True,
        markup=True,
        show_time=False,
        show_path=False,
    )
    console_handler.setLevel(level)
    console_format = logging.Formatter("%(message)s")
    console_handler.setFormatter(console_format)
    root_logger.addHandler(console_handler)

    # 2. File handler with rotation (keep up to 5 files of 10 MB each)
    file_handler = RotatingFileHandler(
        os.path.join(_LOG_DIR, "empire.log"),
        maxBytes=10_485_760,  # 10 MB
        backupCount=5,
    )
    file_handler.setLevel(level)
    file_format = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    file_handler.setFormatter(file_format)
    root_logger.addHandler(file_handler)

    logging.info("📋 Logging system initialised.")
