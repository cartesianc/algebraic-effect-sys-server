"""Create a runnable SDK namespace, or a new Cabal workspace with the SDK sources."""

import json
import os
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from .model import constructor_name
from .entries import business_dir, entries
from ..runtime.snapshot import RegistrationConflict


def namespace_parts(namespace: str):
    parts = namespace.split(".")
    if not all(constructor_name(p) and not (os.name == "nt" and os.path.isreserved(p)) for p in parts):
        raise ValueError("命名空间须为大写开头的 Haskell 模块名，可用点号分层。")
    if "Generated" in parts:
        raise ValueError("Generated 是 SDK 的生成目录。")
    return parts


def write_namespace(folder: Path, ns: str):
    files = {
        "Types.hs": f"module {ns}.Types where\n",
        "Effects.hs": f"""{{-# LANGUAGE DuplicateRecordFields #-}}
{{-# LANGUAGE TypeApplications #-}}
module {ns}.Effects (effects) where

import Framework.SDK
import {ns}.Types

effects :: [EffectSpec]
effects =
  [ EffectSpec
      {{ name = "Start"
      , typing = Excu {{ handler = Just "startWorkflow" }}
      , input = "Root"
      , demands = []
      }}
  ]
""",
        "AST.hs": f"""module {ns}.AST (ast) where

import Framework.SDK (AST, chain, node)

ast :: AST
ast = chain [node "Start"]
""",
        "Handlers.hs": f"""{{-# LANGUAGE FlexibleContexts #-}}
{{-# LANGUAGE FlexibleInstances #-}}
{{-# LANGUAGE UndecidableInstances #-}}
module {ns}.Handlers () where

import Framework.SDK (rawoutput)
import {ns}.Generated.Effects

instance StartWorkflowLayer m => StartWorkflowHandler m where
  startWorkflow = rawoutput []
""",
        "Main.hs": f"""module {ns}.Main (main) where

import {ns}.AST (ast)
import {ns}.Effects (effects)
import {ns}.Generated.Program (interpreter)

main :: IO ()
main = interpreter ast effects
""",
    }
    folder.mkdir()
    for filename, source in files.items():
        (folder / filename).write_text(source, encoding="utf-8")


def create_namespace(root: Path, namespace: str, source_dir: str | None = None, *, found=None):
    if found is None:
        found = entries(root)
    if namespace in found:
        raise RegistrationConflict("命名空间已存在。")
    if source_dir is None:
        roots = {entry.source_dir for entry in found.values()}
        if len(roots) > 1:
            raise ValueError("项目有多个业务源码目录，请指定新命名空间的源码目录。")
        source_dir = next(iter(roots), Path("src")).as_posix()
    source = business_dir(root, source_dir)
    dest = root.joinpath(source, *namespace_parts(namespace))
    if dest.resolve() != dest:
        raise ValueError("命名空间路径超出项目目录。")
    if dest.exists():
        raise RegistrationConflict("命名空间目录已存在。")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=dest.parent) as temporary:
        stage = Path(temporary) / "namespace"
        write_namespace(stage, namespace)
        if dest.exists():
            raise RegistrationConflict("命名空间目录已存在。")
        try:
            stage.rename(dest)
        except OSError as error:
            if dest.exists():
                raise RegistrationConflict("命名空间目录已存在。") from error
            raise
    return {"namespace": namespace, "source_dir": source.as_posix()}


SDK_ARCHIVE = Path(__file__).with_name("sdk.zip")


def unpack_sdk(bundle: ZipFile, stage: Path):
    bundle.extractall(stage, [p for p in bundle.namelist() if p.startswith("sdk/")])


def restore_sdk(root: Path):
    archive = root / "sdk.zip"
    if archive.resolve() != archive or not archive.is_file() or archive.read_bytes() != SDK_ARCHIVE.read_bytes():
        raise ValueError("请将匹配此项目的 SDK 放入 sdk/ 后再构建。")
    with TemporaryDirectory(dir=root) as temp, ZipFile(SDK_ARCHIVE) as bundle:
        unpack_sdk(bundle, Path(temp))
        (Path(temp) / "sdk").rename(root / "sdk")


def scaffold_project(stage: Path, sdk_root: Path | None, namespace: str, source_dir: str, install_sdk: bool):
    """Keep the SDK tool and runtime stanza from the selected version. Omit demo applications."""
    parts = namespace_parts(namespace)
    if sdk_root:
        source_sdk = sdk_root if (sdk_root / "Framework/SDK.hs").is_file() else sdk_root / "sdk"
        if not (source_sdk / "Framework/SDK.hs").is_file():
            raise ValueError("SDK 模板中缺少 sdk/Framework/SDK.hs。")
        texts = [p.read_text(encoding="utf-8-sig") for p in source_sdk.parent.glob("*.cabal")]
        if install_sdk:
            shutil.copytree(source_sdk, stage / "sdk", ignore=shutil.ignore_patterns("*.hi", "*.o", "*.dyn_hi", "*.dyn_o"))
        if (source_sdk.parent / "LICENSE").is_file():
            shutil.copyfile(source_sdk.parent / "LICENSE", stage / "LICENSE")
    else:
        texts = []
    if not texts:
        with ZipFile(SDK_ARCHIVE) as bundle:
            texts = [bundle.read(p).decode("utf-8-sig") for p in bundle.namelist() if p.endswith(".cabal")]
            if not (stage / "LICENSE").is_file():
                bundle.extract("LICENSE", stage)
            if not sdk_root and install_sdk:
                unpack_sdk(bundle, stage)
    manifests = []
    for text in texts:
        stanzas = re.split(r"(?=^\S)", text, flags=re.M)
        runtime = next((s for s in stanzas if s.splitlines()[:1] == ["common runtime-program"]), None)
        tool = next((s for s in stanzas if s.splitlines()[:1] == ["executable effect-codegen"]), None)
        if runtime and tool:
            manifests.append((runtime.strip(), tool.strip()))
    if len(manifests) != 1:
        raise ValueError("SDK 模板需要 sdk/Framework/SDK.hs 及声明 runtime-program 和 effect-codegen 的 Cabal 文件。")
    runtime, tool = manifests[0]
    source = business_dir(stage, source_dir).as_posix()
    runtime, count = re.subn(r"(?mi)^([ \t]*hs-source-dirs\s*:)[^\n]*$",
                             lambda m: m[1] + " " + json.dumps(source, ensure_ascii=False) + ", sdk", runtime)
    if count != 1:
        raise ValueError("SDK 的 runtime-program 须声明一个 hs-source-dirs 字段。")
    (stage / "cabal.project").write_text("packages: .\nwrite-ghc-environment-files: always\n", encoding="utf-8")
    (stage / ".gitignore").write_text("dist-newstyle/\n.ghc.environment.*\n", encoding="utf-8")
    (stage / "business-app.cabal").write_text(
        "cabal-version: 3.0\nname: business-app\nversion: 0.1.0.0\nbuild-type: Simple\n\n"
        + runtime + "\n\n" + tool + "\n\nexecutable business-app\n  import: runtime-program\n"
        + "  main-is: " + "/".join(parts) + "/Main.hs\n  ghc-options: -main-is " + namespace + ".Main\n"
        + "  other-modules:\n" + "".join(f"      {namespace}.{module}\n" for module in
              ["AST", "Effects", "Types", "Generated.Program", "Generated.Handlers", "Generated.Prototype", "Generated.Effects"]),
        encoding="utf-8")
    create_namespace(stage, namespace, source)
