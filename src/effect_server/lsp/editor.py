"""One native HLS session per open handwritten editor. Only the server maps files to URIs."""

import asyncio
from collections import deque
from contextlib import suppress
import os
from pathlib import Path
import re
import subprocess

from fastapi import WebSocket, WebSocketDisconnect
from watchfiles import DefaultFilter, awatch

from .analysis.sources import modules
from .project.hls import hls_tool, read_message, write_message
from .runtime.commands import stop_process


TOKEN_TYPES = ["namespace", "type", "class", "enum", "interface", "typeParameter", "parameter",
               "variable", "property", "enumMember", "function", "method", "macro", "operator"]
HLS_SETTINGS = {"haskell": {"plugin": {"semanticTokens": {"globalOn": True}}}}


def editor_setting(section):
    value = HLS_SETTINGS
    for key in section.split(".") if section else ():
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def match_glob(path: Path, pattern: str) -> bool:
    group = re.search(r"\{([^{}]*)\}", pattern)
    if group:
        return any(match_glob(path, pattern[:group.start()] + alt + pattern[group.end():])
                   for alt in group[1].split(","))
    return path.full_match(pattern)


async def editor_session(ws: WebSocket, root: Path, folder: Path):
    tool = hls_tool()
    if not tool:
        await ws.send_json({"type": "unavailable", "reason": "missing", "message": "服务端未找到 HLS。请在运行后端的机器上安装，或将 EFFECT_HLS 指向已有的可执行文件。"})
        return
    files = await asyncio.to_thread(modules, folder, False)
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    try:
        process = subprocess.Popen([tool, "--lsp"], cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, **flags)
    except OSError as err:
        await ws.send_json({"type": "unavailable", "message": f"HLS 无法启动：{err}"})
        return
    writes = asyncio.Lock()
    ready = asyncio.Event()
    shutdown = asyncio.Event()
    versions = {}
    pending = {}
    watches = {}
    last_log = deque(maxlen=5)
    opened = {}
    watch_requested = asyncio.Event()
    token_legend = []
    token_id = -2  # Positive IDs belong to browser queries, -1 to shutdown.

    def drain_log():
        for line in process.stderr:
            last_log.append(line.decode("utf-8", errors="replace").strip()[-500:])

    async def send(**message):
        async with writes:
            await asyncio.to_thread(write_message, process.stdin, message)

    async def server_request(message):
        method = message["method"]
        params = message.get("params", {})
        result = None
        if method == "workspace/configuration":
            result = [editor_setting(item.get("section")) for item in params.get("items", [])]
        elif method == "workspace/workspaceFolders":
            result = [{"uri": root.as_uri(), "name": root.name}]
        elif method == "client/registerCapability":
            for item in params.get("registrations", []):
                if item["method"] == "workspace/didChangeWatchedFiles":
                    watches[item["id"]] = item["registerOptions"]["watchers"]
                    watch_requested.set()
        elif method == "client/unregisterCapability":
            for item in params.get("unregisterations", []):
                watches.pop(item["id"], None)
        elif method not in {"window/workDoneProgress/create", "window/showMessageRequest"}:
            await send(id=message["id"], error={"code": -32601, "message": "Unsupported editor request"})
            return
        await send(id=message["id"], result=result)

    def definition(result):
        for item in result if isinstance(result, list) else [result] if result else []:
            uri = item.get("uri") or item.get("targetUri", "")
            if not uri.startswith("file:"):
                continue
            path = Path.from_uri(uri).resolve()
            loc = item.get("targetSelectionRange") or item.get("range")
            if not loc or not path.is_relative_to(root) or path.suffix != ".hs":
                continue
            if path in files.values():
                return {"file": path.relative_to(folder).as_posix(), "range": loc}
            if path.is_file():
                lines = path.read_text(encoding="utf-8-sig").splitlines()
                line = loc["start"]["line"]
                return {"path": path.relative_to(root).as_posix(), "line": line + 1,
                        "preview": "\n".join(lines[max(0, line-1):line+5])}
        return None

    async def from_hls():
        nonlocal token_legend
        while True:
            msg = await asyncio.to_thread(read_message, process.stdout)
            if "method" in msg:
                if "id" in msg:
                    await server_request(msg)
                elif msg["method"] == "textDocument/publishDiagnostics":
                    params = msg["params"]
                    name = opened.get(Path.from_uri(params["uri"]).resolve()) if params["uri"].startswith("file:") else None
                    if name:
                        await ws.send_json({"type": "diagnostics", "file": name, "version": params.get("version"),
                                            "items": params["diagnostics"]})
                continue
            number = msg.get("id")
            if number == 0:
                if "error" in msg:
                    raise ValueError(msg["error"]["message"])
                provider = msg["result"]["capabilities"].get("semanticTokensProvider") or {}
                if provider.get("full") or isinstance(provider.get("full"), dict):
                    token_legend = provider["legend"]["tokenTypes"]
                await send(method="initialized", params={})
                ready.set()
                await ws.send_json({"type": "ready"})
            elif number == -1:
                shutdown.set()
                return
            elif number in pending:
                kind, name, version = pending.pop(number)
                result = msg.get("result")
                if kind == "tokens":
                    if versions.get(name) == version and isinstance(result, dict) and "data" in result:
                        await ws.send_json({"type": kind, "file": name, "version": version,
                                            "legend": token_legend, "data": result["data"]})
                    continue
                elif kind == "definition":
                    result = definition(result)
                await ws.send_json({"type": kind, "id": number, "file": name, "version": version,
                                    "result": result, "error": (msg.get("error") or {}).get("message")})

    async def from_browser():
        nonlocal token_id
        while True:
            msg = await ws.receive_json()
            kind, name = msg.get("type"), msg.get("file")
            if not ready.is_set() or name not in files:
                continue
            path = files[name]
            document = {"uri": path.as_uri()}
            version = msg.get("version")
            if not isinstance(version, int) or version < 1:
                continue
            if kind == "sync" and isinstance(msg.get("text"), str) and version > versions.get(name, 0):
                if name not in versions:
                    await send(method="textDocument/didOpen", params={"textDocument": {
                        **document, "version": version, "languageId": "haskell", "text": msg["text"]}})
                    opened[path] = name
                else:
                    await send(method="textDocument/didChange", params={"textDocument": {**document, "version": version},
                               "contentChanges": [{"text": msg["text"]}]})
                versions[name] = version
                if token_legend:
                    for old, (previous, filename, _) in list(pending.items()):
                        if previous == "tokens" and filename == name:
                            del pending[old]
                            await send(method="$/cancelRequest", params={"id": old})
                    pending[token_id] = ("tokens", name, version)
                    await send(id=token_id, method="textDocument/semanticTokens/full", params={"textDocument": document})
                    token_id -= 1
            elif version == versions.get(name):
                if kind == "saved":
                    await send(method="textDocument/didSave", params={"textDocument": document})
                elif kind in {"hover", "definition"}:
                    pos, number = msg.get("position", {}), msg.get("id")
                    if (not isinstance(number, int) or number < 1 or number in pending
                            or not all(isinstance(pos.get(k), int) and pos[k] >= 0 for k in ("line", "character"))):
                        continue
                    for old, (previous, _, _) in list(pending.items()):
                        if previous == kind:
                            del pending[old]
                            await send(method="$/cancelRequest", params={"id": old})
                    pending[number] = kind, name, version
                    await send(id=number, method="textDocument/" + kind, params={"textDocument": document, "position": pos})

    async def watched():
        await watch_requested.wait()
        ignore = DefaultFilter(ignore_dirs=(*DefaultFilter.ignore_dirs, "dist-newstyle", ".console", ".hie"))
        async for batch in awatch(root, watch_filter=ignore, debounce=250):
            if not watches:
                continue
            changes = []
            for change, filename in batch:
                path = Path(filename)
                bit = 1 << (change.value - 1)
                if any(isinstance(pattern := watch["globPattern"], str) and (watch.get("kind", 7) & bit)
                       and (match_glob(path.relative_to(root), pattern) or match_glob(path, pattern))
                       for watchers in watches.values() for watch in watchers):
                    changes.append({"uri": path.as_uri(), "type": change.value})
            if changes:
                await send(method="workspace/didChangeWatchedFiles", params={"changes": changes})

    log = asyncio.create_task(asyncio.to_thread(drain_log))
    reader = asyncio.create_task(from_hls())
    browser = asyncio.create_task(from_browser())
    watcher = asyncio.create_task(watched())
    started = asyncio.create_task(asyncio.wait_for(ready.wait(), 90))
    try:
        await send(id=0, method="initialize", params={"processId": os.getpid(), "rootUri": root.as_uri(),
                   "initializationOptions": HLS_SETTINGS,
                   "workspaceFolders": [{"uri": root.as_uri(), "name": root.name}],
                   "capabilities": {"workspace": {"configuration": True, "workspaceFolders": True,
                                   "didChangeWatchedFiles": {"dynamicRegistration": True}},
                                   "general": {"positionEncodings": ["utf-16"]},
                                   "textDocument": {"semanticTokens": {"requests": {"full": True},
                                                    "tokenTypes": TOKEN_TYPES, "tokenModifiers": [], "formats": ["relative"],
                                                    "overlappingTokenSupport": False, "multilineTokenSupport": False},
                                                    "publishDiagnostics": {"versionSupport": True},
                                                    "hover": {"contentFormat": ["plaintext", "markdown"]}}}})
        tasks = {reader, browser, watcher, started}
        while tasks:
            done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if done != {started}:
                break
    except WebSocketDisconnect:
        pass
    except (OSError, ValueError, EOFError, TimeoutError) as err:
        with suppress(WebSocketDisconnect, RuntimeError, OSError):
            await ws.send_json({"type": "unavailable", "message": "HLS 启动或连接失败。请检查项目 GHC 对应的 HLS 版本。"
                                + ("\n" + ("\n".join(last_log) or str(err)) if last_log or str(err) else "")})
    finally:
        for task in (browser, watcher, started):
            task.cancel()
        if ready.is_set() and process.poll() is None and not reader.done():
            with suppress(OSError, TimeoutError, subprocess.TimeoutExpired):
                await send(id=-1, method="shutdown", params=None)
                await asyncio.wait_for(shutdown.wait(), 1.5)
                await send(method="exit")
                await asyncio.to_thread(process.wait, 1)
        await stop_process(process)
        reader.cancel()
        await asyncio.gather(reader, browser, started, watcher, log, return_exceptions=True)
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()
