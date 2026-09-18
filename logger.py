# logger.py
# logger.py
"""
Central logging configuration for the Empire system.
Provides both console output (via Rich) and rotating file logs.
"""

import logging
import os
from logging.handlers import RotatingFileHandler

try:
    from rich.logging import RichHandler
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


def setup_logging(level=logging.INFO, log_dir: str = None):
    """
    Initialise the root logger with console and file handlers.

    Args:
        level: Logging level (e.g., logging.DEBUG, logging.INFO).
        log_dir: Directory where logs will be stored.
                 If None, defaults to <cwd>/ai_civilization/logs.
    """
    if log_dir is None:
        log_dir = os.path.join(os.getcwd(), "ai_civilization", "logs")
    os.makedirs(log_dir, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove any existing handlers to avoid duplicates (e.g., on reload)
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # 1. Console handler (Rich if available)
    if RICH_AVAILABLE:
        console_handler = RichHandler(
            rich_tracebacks=True,
            tracebacks_show_locals=True,
            markup=True,
            show_time=False,
            show_path=False,
        )
        console_handler.setLevel(level)
        console_handler.setFormatter(logging.Formatter("%(message)s"))
    else:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        console_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
    root_logger.addHandler(console_handler)

    # 2. File handler with rotation (up to 5 files of 10 MB each)
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "empire.log"),
        maxBytes=10_485_760,   # 10 MB
        backupCount=5,
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - [%(threadName)s] - %(message)s"
        )
    )
    root_logger.addHandler(file_handler)

    logging.info("📋 Logging system initialised.")


def get_logger(name: str = None) -> logging.Logger:
    """
    Return a logger instance. If name is None, returns the root logger.
    """
    if name:
        return logging.getLogger(name)
    return logging.getLogger()
