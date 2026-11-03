"""Size-limited logs, so log files stay small enough to copy off a machine."""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

MAX_BYTES = 10 * 1024 * 1024
FILES_KEPT = 3


def get_logger(log_dir: Path, name: str) -> logging.Logger:
    logger = logging.getLogger(f"livingcode.{name}")
    path = log_dir / f"{name}.log"
    if any(getattr(h, "baseFilename", "") == str(path) for h in logger.handlers):
        return logger
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=FILES_KEPT - 1)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def append_limited(path: Path, line: str, max_bytes: int = MAX_BYTES, files_kept: int = FILES_KEPT) -> None:
    """Appends one line and rotates the file once it's over the limit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size >= max_bytes:
            for number in range(files_kept - 1, 0, -1):
                older = path.with_name(f"{path.name}.{number}")
                newer = path.with_name(f"{path.name}.{number - 1}") if number > 1 else path
                if newer.exists():
                    os.replace(newer, older)
    except FileNotFoundError:
        pass
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line.rstrip("\n") + "\n")
