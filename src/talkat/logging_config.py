"""Logging configuration for Talkat."""

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

# Default log format
DEFAULT_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
SIMPLE_FORMAT = "%(levelname)s: %(message)s"

# Log file location
LOG_DIR = Path.home() / ".local" / "share" / "talkat" / "logs"


def setup_logging(
    verbose: bool = False,
    quiet: bool = False,
    log_file: Optional[str] = None,
    log_to_file: bool = True,
) -> None:
    """
    Configure logging for the application.
    
    Args:
        verbose: Enable verbose (DEBUG) logging
        quiet: Suppress all but ERROR messages
        log_file: Custom log file path
        log_to_file: Whether to log to file in addition to console
    """
    # Determine log level
    if quiet:
        level = logging.ERROR
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO
    
    # Create logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    # Remove any existing handlers
    root_logger.handlers.clear()
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(level)
    
    # Use simple format for console
    console_formatter = logging.Formatter(SIMPLE_FORMAT if not verbose else DEFAULT_FORMAT)
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)
    
    # File handler (if enabled)
    if log_to_file:
        if log_file:
            log_path = Path(log_file)
        else:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_path = LOG_DIR / "talkat.log"
        
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
        )
        file_handler.setLevel(logging.DEBUG)  # Always log everything to file
        file_formatter = logging.Formatter(DEFAULT_FORMAT)
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)
    
    # Suppress noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    
    # Suppress ALSA warnings specifically
    logging.getLogger("pyaudio").setLevel(logging.ERROR)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for a module.
    
    Args:
        name: The name of the module (usually __name__)
    
    Returns:
        A configured logger instance
    """
    return logging.getLogger(name)