"""Read the SDK's generated workflow or inspect the current AST without generating."""

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

from anyio import CancelScope

from ..framework import Framework
from ..project.model import constructor_name
from .commands import start_process, stop_process


def ast_namespaces(root: Path):
    demo = root / "demo"
    for ast in demo.rglob("AST.hs"):
        parts = ast.parent.relative_to(demo).parts
        if parts and all(map(constructor_name, parts)) and ast.resolve().is_relative_to(demo) and ast.is_file():
            yield ".".join(parts)


def source_digest(path: Path) -> str:
    # 与 SDK 写入 sources 的算法一致：CRLF 换成 LF 后取 MD5，不随检出的换行方式变化。
    return hashlib.md5(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def current_sources(root: Path, folder: Path) -> dict[str, str]:
    """Digest every handwritten module of the namespace, keyed like report sources.

    The page compares an optimized report's ``sources`` with this map, so a preview
    taken before the latest edit is dropped without rerunning the SDK."""
    generated = folder / "Generated"
    return {
        path.relative_to(root).as_posix(): source_digest(path)
        for path in sorted(folder.rglob("*.hs"))
        if path.is_file() and not path.is_relative_to(generated)
    }


def generated_workflow(root: Path, namespace: str) -> dict:
    folder = root.joinpath("demo", *namespace.split(".")).resolve()
    if not folder.is_relative_to(root.resolve()):
        raise ValueError("命名空间目录超出绑定的工作树。")
    current = current_sources(root.resolve(), folder)
    return {**report_state(root, folder, namespace), "current": current}


def report_state(root: Path, folder: Path, namespace: str) -> dict:
    path = (folder / "Generated" / "Workflow.json").resolve()
    if not path.is_relative_to(root):
        raise ValueError("工作流报告超出绑定的工作树。")
    if not path.is_file():
        return {"state": "missing"}
    report = json.loads(path.read_bytes())
    if report["schemaVersion"] != 1 or report["namespace"] != namespace or not report["sources"]:
        raise ValueError("工作流报告与当前命名空间或格式版本不符。")
    for entry in report["sources"]:
        source = (root / entry["file"]).resolve()
        if not source.is_relative_to(folder):
            raise ValueError("工作流源码超出命名空间目录。")
        try:
            digest = source_digest(source)
        except FileNotFoundError:
            digest = None
        if digest != entry["md5"]:
            return {"state": "stale", "report": report}
    return {"state": "fresh", "report": report}


class AstLoadError(ValueError):
    pass


async def optimize_workflow(framework: Framework, namespace: str) -> dict:
    process = start_process(framework, [framework.executable, "ast", namespace], stderr=subprocess.PIPE)
    output = asyncio.create_task(asyncio.to_thread(process.communicate))
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(output), timeout=120)
    finally:
        with CancelScope(shield=True):
            await stop_process(process)
            await output
    if process.returncode:
        raise AstLoadError(stderr.decode("utf-8", errors="replace").strip()
                           or f"effect-codegen exited with {process.returncode}")
    return json.loads(stdout)
