"""Publish complete SDK snapshots for the console."""

import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

from ..framework import Framework
from ..project.model import constructor_name


project_directory: ContextVar[Path | None] = ContextVar("console_project_directory", default=None)


def console_home() -> Path:
    return Path(os.environ.get("EFFECT_CONSOLE_DIR", ".console")).resolve()


def directory() -> Path:
    return project_directory.get() or console_home()


class RegistrationConflict(ValueError):
    pass


@contextmanager
def namespace_lock(namespace: str):
    # OS releases the lock if the worker terminates. Keep the inode for other workers.
    dest = directory()
    dest.mkdir(parents=True, exist_ok=True)
    with (dest / (namespace + ".lock")).open("a+b") as handle:
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RegistrationConflict("该命名空间有任务正在进行，请稍后重试。") from error
        yield


def stamp(namespace: str, data: dict) -> dict:
    if data["namespace"] != namespace or data["schemaVersion"] != 1:
        raise ValueError("Unexpected console snapshot")
    data["analyzedAt"] = datetime.now(timezone.utc).isoformat()
    return data


def atomic_write(target: Path, content: bytes) -> None:
    with NamedTemporaryFile(dir=target.parent, delete=False) as handle:
        staging = Path(handle.name)
    try:
        staging.write_bytes(content)
        if target.exists():
            staging.chmod(target.stat().st_mode)
        staging.replace(target)
    finally:
        staging.unlink(missing_ok=True)


def store(data: dict) -> Path:
    dest = directory()
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / (data["namespace"] + ".json")
    atomic_write(target, json.dumps(data, ensure_ascii=False).encode("utf-8"))
    return target


def publish(namespace: str, framework: Framework) -> Path:
    if not all(constructor_name(part) for part in namespace.split(".")):
        raise ValueError("Invalid namespace")
    with namespace_lock(namespace):
        return store(stamp(namespace, framework.read_json("console", namespace)))
