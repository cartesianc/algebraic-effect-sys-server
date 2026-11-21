"""Persistent local project bindings; selection belongs to the requesting tab."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
import os
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory

from .scaffold import scaffold_project
from ..runtime.snapshot import RegistrationConflict, console_home, namespace_lock


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    root: str
    executable: str


def project_id(root: Path):
    return sha256(os.path.normcase(str(root)).encode()).hexdigest()[:24]


def project_at(root: Path, executable: Path | None, name: str = "") -> Project:
    return Project(project_id(root), name.strip() or root.name, str(root), str(executable) if executable else "")


def describe(project: Project):
    return {**asdict(project), "sdkReady": Path(project.executable).is_file()}


def startup_project():
    root, exe = os.getenv("EFFECT_FRAMEWORK_ROOT"), os.getenv("EFFECT_CODEGEN")
    if root and exe:
        return project_at(Path(root).resolve(), Path(exe).resolve())


@contextmanager
def database():
    home = console_home()
    home.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(home / "projects.sqlite3")
    try:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS project (id TEXT PRIMARY KEY, name TEXT, root TEXT, executable TEXT)")
            yield db
    finally:
        db.close()


def projects():
    with database() as db:
        saved = [Project(*row) for row in db.execute("SELECT id,name,root,executable FROM project ORDER BY name,id")]
    default = startup_project()
    if default and not any(p.id == default.id for p in saved):
        saved.insert(0, default)
    return [describe(p) for p in saved]


def lookup(project_id: str) -> Project:
    with database() as db:
        row = db.execute("SELECT id,name,root,executable FROM project WHERE id=?", (project_id,)).fetchone()
    if row:
        return Project(*row)
    default = startup_project()
    if default and default.id == project_id:
        return default
    raise FileNotFoundError("找不到项目。")


def absolute(value: str) -> Path:
    path = Path(value).expanduser()
    if not value.strip() or not path.is_absolute():
        raise ValueError("请提供本机绝对路径。")
    return path.resolve()


def sdk_executable(root: Path, override: str | None, *, allow_unbuilt=False):
    if override:
        exe = absolute(override)
    else:
        default = startup_project()
        if default and Path(default.root) == root and Path(default.executable).is_file():
            return Path(default.executable)
        filename = "effect-codegen.exe" if os.name == "nt" else "effect-codegen"
        builds = (root / "dist-newstyle/build").glob("**/x/effect-codegen/build/effect-codegen/" + filename)
        exe = max(builds, key=lambda p: p.stat().st_mtime_ns, default=None)
        if exe is None:
            with database() as db:
                known = db.execute("SELECT executable FROM project WHERE id=?", (project_id(root),)).fetchone()
            if known and Path(known[0]).is_file():
                exe = Path(known[0])
            elif allow_unbuilt:
                return None
    if not exe or not exe.is_file() or exe.stem != "effect-codegen" or not os.access(exe, os.X_OK):
        raise ValueError("找不到已构建的 effect-codegen。请指定工具路径，或在 SDK 目录执行 cabal build exe:effect-codegen。")
    return exe


def persist(project: Project):
    with database() as db:
        db.execute("INSERT INTO project VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                   "name=excluded.name, executable=excluded.executable",
                   (project.id, project.name, project.root, project.executable))
    return describe(project)


def import_project(root: str, name: str, executable: str | None):
    folder = absolute(root)
    if not (folder / "sdk/Framework/SDK.hs").is_file() or not (folder / "demo").is_dir():
        raise ValueError("项目目录应包含 sdk/Framework/SDK.hs 和 demo/。")
    return persist(project_at(folder, sdk_executable(folder, executable, allow_unbuilt=True), name))


def configure_sdk(root: Path, *, build: bool):
    flags = [] if build else ["--only-configure"]
    result = subprocess.run(["cabal", "build", *flags, "exe:effect-codegen"], cwd=root,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or "Cabal 初始化失败。")


def build_sdk(project: Project):
    root = Path(project.root)
    with namespace_lock("__sdk-build"):
        configure_sdk(root, build=True)
        return persist(project_at(root, sdk_executable(root, None), project.name))


def new_project(root: str, name: str, namespace: str, sdk_root: str, executable: str | None):
    target, template = absolute(root), absolute(sdk_root)
    if target.exists():
        raise RegistrationConflict("新项目需要一个尚不存在的目录；已有项目请使用导入。")
    if not target.parent.is_dir() or target.is_relative_to(template / "sdk"):
        raise ValueError("请选择已存在的父目录，并将新项目放在 SDK 源码目录之外。")
    exe = sdk_executable(template, executable, allow_unbuilt=True)
    with TemporaryDirectory(dir=target.parent) as staging:
        folder = Path(staging) / "project"
        folder.mkdir()
        scaffold_project(folder, template, namespace)
        # A fresh Cabal checkout needs its local package database before GHCi
        # can load the first namespace. A prebuilt SDK only needs configuration.
        configure_sdk(folder, build=exe is None)
        if exe is None:
            exe = target / sdk_executable(folder, None).relative_to(folder)
        if target.exists():
            raise RegistrationConflict("项目目录已存在。")
        folder.rename(target)
        try:
            return persist(project_at(target, exe, name))
        except sqlite3.Error:
            # Reimport remains possible if persistence fails; preserve the completed project.
            raise ValueError("项目已创建，但项目登记失败。可通过导入已有项目重新登记。")
