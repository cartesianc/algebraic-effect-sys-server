"""Persistent local project bindings; selection belongs to the requesting tab."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
from ntpath import isreserved
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import sqlite3
import subprocess
from tempfile import TemporaryDirectory

from .scaffold import SDK_ARCHIVE, create_namespace, namespace_parts, restore_sdk, scaffold_project
from ..framework import Framework
from .entries import entries
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
    template = os.getenv("EFFECT_SDK_TEMPLATE") if project.root == os.getenv("EFFECT_FRAMEWORK_ROOT") else None
    return {**asdict(project), "sdkReady": Path(project.executable).is_file(), "sdkTemplate": template or project.root}


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
    if not (folder / "sdk/Framework/SDK.hs").is_file():
        raise ValueError("项目根目录中找不到 sdk/Framework/SDK.hs。")
    if not entries(folder):
        raise ValueError("无法解析工作流 main 引用的 interpreter、ast 和 effects。请核对导入；经过重导出时需由 HLS 加载项目。")
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
        if not (root / "sdk").exists():
            restore_sdk(root)
        if not (root / "sdk/Framework/SDK.hs").is_file():
            raise ValueError("已有 sdk/ 不完整，请检查 sdk/Framework/SDK.hs。")
        configure_sdk(root, build=True)
        return persist(project_at(root, sdk_executable(root, None), project.name))


def new_project(target: Path, name: str, namespace: str, sdk_root: str | None, executable: str | None,
                source_dir: str | None, install_sdk: bool, *, share_workspace: bool = False):
    template = absolute(sdk_root) if sdk_root else None
    template_source = (template if (template / "Framework/SDK.hs").is_file() else template / "sdk") if template else None
    if not target.name or not target.parent.is_dir() or target.resolve() != target or (template_source and target.is_relative_to(template_source)):
        raise ValueError("请选择有效的项目父目录。")
    with namespace_lock("__create-" + project_id(target)):
        sdk = target / "sdk"
        if sdk.exists() and (sdk.resolve() != sdk or not (sdk / "Framework/SDK.hs").is_file()):
            raise ValueError("已有 sdk/ 不完整，请检查 sdk/Framework/SDK.hs。")
        if share_workspace and (sdk.is_dir() or (target / "sdk.zip").is_file()):
            found = entries(target)
            if found or any(target.glob("*.cabal")):
                try:
                    project = lookup(project_id(target))
                except FileNotFoundError:
                    display = target.name if len(found) > 1 else next(iter(found), namespace)
                    project = project_at(target, None, display)
                exe = sdk_executable(target, None, allow_unbuilt=True)
                created = create_namespace(target, namespace, source_dir, found=found)
                return finish_project(target, project.name, namespace, Path(created["source_dir"]), exe,
                                      install_sdk=install_sdk)
        if target.exists() and (not target.is_dir() or any(p.name not in {"sdk", "sdk.zip"} for p in target.iterdir())):
            raise RegistrationConflict("请选择空文件夹、仅含 SDK 的目录或已有工作区。")
        source_dir = source_dir or "src"
        exe = sdk_executable(template_source.parent if template_source and not sdk.exists() else target,
                             executable, allow_unbuilt=True) if template or executable else None
        with TemporaryDirectory(dir=target if target.is_dir() else target.parent) as staging:
            install_project_files(target, Path(staging), template, namespace, source_dir, install_sdk)
        return finish_project(target, name, namespace, Path(source_dir), exe, install_sdk=sdk.is_dir(),
                              configure=exe is not None)


def install_project_files(target, staging, template, namespace, source_dir, install_sdk):
    sdk = target / "sdk"
    folder = staging / "project"
    folder.mkdir()
    scaffold_project(folder, None if sdk.is_dir() else template, namespace, source_dir, install_sdk and not sdk.exists())
    if not (target / "sdk.zip").exists():
        shutil.copyfile(SDK_ARCHIVE, folder / "sdk.zip")
    if not target.exists():
        folder.rename(target)
        return
    moved = []
    try:
        for path in folder.iterdir():
            if (target / path.name).exists():
                raise RegistrationConflict(f"项目中已存在 {path.name}。")
            moved.append(path.rename(target / path.name))
    except Exception:
        for path in reversed(moved):
            path.rename(folder / path.name)
        raise


def finish_project(target, name, namespace, source_dir, exe, *, install_sdk, configure=False):
    setup_error = None
    try:
        if exe is None and install_sdk:
            if not (target / "sdk").exists():
                restore_sdk(target)
            configure_sdk(target, build=True)
            exe = sdk_executable(target, None)
        elif configure:
            configure_sdk(target, build=False)
        if exe:
            Framework(exe, target, source_dir).run("compiler", namespace)
    except (ValueError, OSError) as error:
        setup_error = str(error)
    try:
        return {**persist(project_at(target, exe, name)), "namespace": namespace, "setupError": setup_error}
    except sqlite3.Error:
        raise ValueError("项目已创建，但项目登记失败。可通过导入已有项目重新登记。")


def create_in_folder(root: str, namespace: str | None, install_sdk: bool, sdk_path: str | None, source_dir: str | None):
    target = absolute(root)
    if namespace is None or not namespace.strip():
        parts = ["".join(word[:1].upper() + word[1:] for word in re.findall(r"[^\W_]+", part))
                 for part in target.name.split(".")]
        namespace = ".".join(parts)
        try:
            namespace_parts(namespace)
        except ValueError:
            namespace = "App"
    namespace = namespace.strip()
    namespace_parts(namespace)
    return new_project(target, namespace, namespace, sdk_path, None, source_dir, install_sdk or bool(sdk_path),
                       share_workspace=True)


def project_target(parent: str, name: str) -> Path:
    if (not name.strip() or name != name.strip() or name in {".", ".."} or name.endswith(".")
            or PureWindowsPath(name).name != name or "/" in name or isreserved(name)
            or any(ord(c) < 32 or c in '<>:"|?*\\' for c in name)):
        raise ValueError("项目名须为有效的单个文件夹名称。")
    return absolute(parent) / name
