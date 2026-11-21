"""Generate, compile and run a published namespace in its bound local checkout."""

import asyncio
import codecs
import json
import os
import re
import signal
import subprocess
from pathlib import Path

from anyio import CancelScope
from fastapi import Request

from ..analysis.prototype import read_prototype
from ..framework import Framework
from ..project.entries import entry_for
from ..project.model import constructor_name
from .snapshot import namespace_lock


def generate_graph(namespace: str, folder: Path, framework: Framework) -> dict:
    with namespace_lock(namespace):
        graph = read_prototype(folder, namespace)
        if graph is None:
            framework.run("workflow", namespace)
            graph = read_prototype(folder, namespace)
            if graph is None:
                raise ValueError("SDK 生成结束，但未找到 Generated/Prototype.hs。")
        return graph


def executable_target(root: Path, main: Path) -> str | None:
    """Resolve main-is against the executable's source directories, including common stanzas."""
    for manifest in root.glob("*.cabal"):
        stanzas = {}
        fields = None
        key = ""
        for raw in manifest.read_text(encoding="utf-8").splitlines():
            line = raw.partition("--")[0].rstrip()
            if not line:
                continue
            if not line[0].isspace():
                match = re.fullmatch(r"(common|executable)\s+([\w-]+)", line, re.I)
                fields = stanzas.setdefault((match[1].lower(), match[2]), {}) if match else None
                key = ""
            elif fields is not None:
                field, colon, value = line.strip().partition(":")
                if colon:
                    key = field.lower()
                if key in {"main-is", "import", "hs-source-dirs"}:
                    fields.setdefault(key, []).extend(t.strip('"') for t in re.findall(r'"[^"]*"|[^,\s]+', value if colon else field))
        for (kind, name), fields in stanzas.items():
            if kind != "executable":
                continue
            dirs = list(fields.get("hs-source-dirs", []))
            pending, seen = list(fields.get("import", [])), set()
            while pending:
                common = pending.pop()
                if common not in seen:
                    seen.add(common)
                    shared = stanzas.get(("common", common), {})
                    dirs.extend(shared.get("hs-source-dirs", []))
                    pending.extend(shared.get("import", []))
            if any(Path(d) / file == main for file in fields.get("main-is", []) for d in dirs or ["."]):
                return "exe:" + name
    return None


async def steps(framework: Framework, ns: str, execute: bool):
    root = framework.root
    entry = await asyncio.to_thread(entry_for, root, ns)
    target = executable_target(root, entry.main)
    yield "生成", framework.command("workflow", ns)
    if target:
        yield "编译", ["cabal", "build", target]
        if execute:
            result = await asyncio.to_thread(subprocess.run, ["cabal", "list-bin", target],
                                             cwd=root, capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise ValueError(result.stderr.strip() or "无法定位编译产物。")
            binary = Path(result.stdout.strip())
    else:
        # Standalone namespaces such as FacadeProbe use the checkout's package environment.
        out = root / "dist-newstyle" / "effect-studio" / ns
        out.mkdir(parents=True, exist_ok=True)
        binary = out / ("program.exe" if os.name == "nt" else "program")
        yield "编译", ["cabal", "exec", "--", "ghc", "--make", "-threaded", "-isdk", "-i" + framework.source_dir.as_posix(),
                       "-main-is", entry.main_module, "-outputdir", str(out), "-o", str(binary),
                       str(entry.main)]
    if execute:
        yield "执行", [str(binary)]


def event(kind: str, **data) -> bytes:
    return (json.dumps({"type": kind, **data}, ensure_ascii=False) + "\n").encode("utf-8")


async def stop_process(process: subprocess.Popen):
    if process.poll() is None:
        if os.name == "nt":
            await asyncio.to_thread(subprocess.run, ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    await asyncio.to_thread(process.wait)


def start_process(framework: Framework, args, *, stderr, stream=False):
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
               else {"start_new_session": True})
    return subprocess.Popen(
        args, bufsize=0, cwd=framework.root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=stderr, env={**os.environ, "EFFECT_CONSOLE_STREAM": "1"} if stream else None, **options,
    )


async def command_output(request: Request, framework: Framework, ns: str, execute: bool):
    try:
        if not all(constructor_name(part) for part in ns.split(".")):
            raise ValueError("无效的命名空间。")
        with namespace_lock(ns):
            async for title, args in steps(framework, ns, execute):
                yield event("stage", text=title)
                # A pipe reader thread also works with Windows selector event loops.
                process = start_process(framework, args, stderr=subprocess.STDOUT, stream=True)
                read = asyncio.create_task(asyncio.to_thread(process.stdout.read, 8192))
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                try:
                    while True:
                        if await request.is_disconnected():
                            return
                        ready, _ = await asyncio.wait([read], timeout=1)
                        if not ready:
                            yield b"\n"
                            continue
                        chunk = read.result()
                        text = decoder.decode(chunk, final=not chunk)
                        if text:
                            yield event("output", text=text)
                        if not chunk:
                            break
                        read = asyncio.create_task(asyncio.to_thread(process.stdout.read, 8192))
                    code = await asyncio.to_thread(process.wait)
                finally:
                    with CancelScope(shield=True):
                        await stop_process(process)
                        await asyncio.gather(read, return_exceptions=True)
                        process.stdout.close()
                if code:
                    yield event("exit", code=code)
                    return
                if title == "编译":
                    yield event("compiled")
            yield event("exit", code=0)
    except (OSError, ValueError, subprocess.SubprocessError, NotImplementedError) as error:
        yield event("error", text=str(error) or type(error).__name__)
