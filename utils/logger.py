"""
Logging setup for PolyBot.
"""

import logging
import sys
from pathlib import Path


def setup_logger(name: str, log_dir: str = "logs") -> logging.Logger:
    # Configure the root "polybot" logger ONCE with handlers.
    # All child loggers (polybot.main, polybot.weather, polybot.clob, etc.)
    # propagate to this root logger and inherit its handlers.
    root_polybot = logging.getLogger("polybot")

    if not root_polybot.handlers:
        root_polybot.setLevel(logging.DEBUG)
        root_polybot.propagate = False  # prevent double-logging via Python root logger

        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # Console handler — INFO level for clean GH Actions logs
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.INFO)
        console.setFormatter(fmt)
        root_polybot.addHandler(console)

        # File handler — DEBUG level for full diagnostics
        log_path = Path(log_dir)
        log_path.mkdir(exist_ok=True)
        file_handler = logging.FileHandler(log_path / "polybot.log")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        root_polybot.addHandler(file_handler)

    return logging.getLogger(name)
