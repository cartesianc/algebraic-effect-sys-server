"""Reconcile saved sources through the same namespace reader as the Facade editor."""

import asyncio
import logging
from pathlib import Path
from subprocess import TimeoutExpired

from . import metadata
from ..analysis.facade import method_definitions
from ..analysis.read import read_namespace
from ..analysis.sources import namespace_folder, revision
from ..codegen.authoring import fingerprint, source_files
from ..codegen.workflow import tree
from ..framework import Framework
from ..project.entries import entries
from ..project.workspaces import projects


logger = logging.getLogger(__name__)
ROLES = {"handler", "parser", "open", "close", "detect", "resolve"}


def contexts(rows, handlers):
    effects = [r["effect"] for r in rows]
    methods = {v for e in effects for k, v in e["typing"].items() if k in ROLES and v}
    definitions = method_definitions(handlers, methods)
    result, assets = {}, {}
    for e in effects:
        name = e["name"]
        if name in result:
            raise ValueError(f"重复注册 {name}")
        result[name] = {"effect": e, "methods": {
            v: [d["source"] for d in definitions[v]] for k, v in e["typing"].items() if k in ROLES and v}}
        if asset := e["typing"].get("asset"):
            contract = asset["contract"]
            ctx = assets.setdefault(contract, {"contract": contract, "consumers": []})
            ctx.update(producer=e, methods=result[name]["methods"])
        for demand in e["demands"]:
            contract = demand["contract"]
            ctx = assets.setdefault(contract, {"contract": contract, "consumers": []})
            ctx["consumers"].append({"effect": name, "access": demand["access"]})
    result.update({"asset:" + contract: {"asset": ctx} for contract, ctx in assets.items()})
    return result


def reconcile(project):
    root, exe = Path(project["root"]), Path(project["executable"])
    if not root.is_dir() or not exe.is_file():
        return
    # A helper imported from outside the namespace can change an evaluated registration.
    versions = []
    found = entries(root, versions=versions)
    versions.append((exe, exe.stat().st_mtime_ns, exe.stat().st_size))
    signature = revision(b"index-assets-v1:" + repr(versions).encode())
    known = metadata.namespaces(project["id"])
    for name, entry in found.items():
        try:
            folder = namespace_folder(root, name, entry.source_dir)
            old = known.get(name)
            if old and old.folder == str(folder) and old.signature == signature:
                continue
            files = source_files(folder)
            source_version = fingerprint(files)
            tree(files.get("Handlers.hs", b""))
            rows = read_namespace(name, Framework(exe, root, entry.source_dir), timeout=60)["registrations"]
            nodes = contexts(rows, files.get("Handlers.hs", b""))
            if fingerprint(source_files(folder)) == source_version:
                metadata.publish(project["id"], name, folder, signature, nodes)
        except (OSError, ValueError, TimeoutExpired) as exc:
            logger.warning("节点索引保留上次结果 %s: %s", name, exc)
    for name, row in known.items():
        if name not in found and not Path(row.folder).exists():
            metadata.remove_namespace(row)


async def watch_projects():
    while True:
        try:
            for project in await asyncio.to_thread(projects):
                try:
                    await asyncio.to_thread(reconcile, project)
                except Exception:
                    logger.exception("无法同步项目 %s", project["id"])
        except Exception:
            logger.exception("无法读取项目列表")
        await asyncio.sleep(8)


async def generate_pending(workbench):
    async with asyncio.TaskGroup() as group:
        for _ in range(4):
            group.create_task(translate_cards(workbench))


async def translate_cards(workbench):
    while True:
        try:
            job = await asyncio.to_thread(metadata.claim)
            if job:
                try:
                    result = await workbench.generate_metadata(job)
                except (ValueError, OSError) as exc:
                    await asyncio.to_thread(metadata.finish, job, None, str(exc))
                else:
                    await asyncio.to_thread(metadata.finish, job, result)
                continue
        except Exception:
            logger.exception("无法更新节点资料")
        await asyncio.sleep(3)
