"""Backups before each deploy. Rollback only ever restores code; data stays a manual step."""
from __future__ import annotations

import os
import shlex
import shutil
import tarfile
import time
from pathlib import Path

from ..common import runner as default_runner


class BackupError(RuntimeError):
    pass


def make(cfg, store, release_id: str, deployment_id: int, source: Path, runner=default_runner.run) -> Path:
    folder = cfg.backups_dir / f"d{deployment_id:05d}-{release_id}"
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / "code.tar.gz"
    try:
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(source, arcname=release_id)
        with tarfile.open(archive, "r:gz") as tar:
            if not tar.getmembers():
                raise BackupError("backup archive is empty")
        shutil.copy2(cfg.path, folder / "config.toml")
    except (OSError, tarfile.TarError) as exc:
        raise BackupError(f"code backup failed: {exc}") from exc
    data_included = bool(cfg.safety.backup_command)
    if data_included:
        env = dict(os.environ, LIVINGCODE_BACKUP_DIR=str(folder))
        done = runner(shlex.split(cfg.safety.backup_command), cwd=cfg.shared_dir, env=env, timeout=300)
        if done.code != 0:
            raise BackupError(f"backup_command failed: {done.output[-500:]}")
    size = sum(item.stat().st_size for item in folder.rglob("*") if item.is_file())
    store.add_backup(release_id, str(folder), size, data_included)
    prune(cfg, store)
    return folder


def prune(cfg, store) -> None:
    for row in store.backups()[cfg.safety.keep_releases:]:
        shutil.rmtree(row["path"], ignore_errors=True)
        store.delete_backup(row["id"])


def restore_code(cfg, store, release_id: str) -> bool:
    """Brings a release folder back from its backup if the folder has gone missing."""
    for row in store.backups():
        archive = Path(row["path"]) / "code.tar.gz"
        if row["release_id"] == release_id and archive.is_file():
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(cfg.releases_dir, filter="data")
            store.audit("guard", "restored_code", {"release": release_id, "at": time.time()})
            return True
    return False
