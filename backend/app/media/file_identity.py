"""Stat identity for bounded, in-process caches of pure media-derived data.
This is not a substitute for export/analysis fingerprint verification.
"""
from pathlib import Path


def file_identity(path: Path) -> tuple:
    resolved=Path(path).resolve()
    stat=resolved.stat()
    return (str(resolved),stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
