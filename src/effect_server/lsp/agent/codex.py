"""Run a draft-only Codex CLI session with the FastAPI service user's login."""

import asyncio
from functools import lru_cache
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

from anyio import CancelScope

from ..runtime.commands import stop_process


@lru_cache(maxsize=1)
def _windows_app_cli():
    # A desktop installation may only put its versioned CLI on PATH inside the app.
    windows = os.environ.get("SystemRoot")
    if not windows:
        return None
    shell = Path(windows) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        package = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-Command",
                                  "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
                                  "(Get-AppxPackage -Name OpenAI.Codex).InstallLocation"],
                                 capture_output=True, timeout=8, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    location = package.stdout.decode("utf-8-sig").strip()
    if location and Path(location).is_absolute():
        return Path(location) / "app/resources/codex.exe"
    return None


def _launcher(path):
    if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
        return None
    path = Path(path).absolute()
    if os.name != "nt" or path.suffix.lower() == ".exe":
        return (str(path),)
    # npm's Windows shim invokes this JS entry. Pass arguments directly to node so
    # that cmd.exe does not reinterpret the multiline instructions and JSON schema.
    entry = path.parent / "node_modules/@openai/codex/bin/codex.js"
    node = shutil.which(str(path.with_name("node.exe"))) or shutil.which("node.exe")
    if path.stem.lower() == "codex" and entry.is_file() and node:
        return (node, str(entry))
    return None


def command():
    override = os.environ.get("EFFECT_CODEX")
    if override:
        return _launcher(shutil.which(os.path.expanduser(override)))
    if os.name == "nt":
        for name in ("codex.exe", "codex.cmd", "codex"):
            if found := _launcher(shutil.which(name)):
                return found
        for key, relative in (("LOCALAPPDATA", "Programs/OpenAI/Codex/bin/codex.exe"), ("APPDATA", "npm/codex.cmd")):
            if directory := os.environ.get(key):
                if found := _launcher(Path(directory) / relative):
                    return found
        installed = _windows_app_cli()
        if installed and not installed.is_file():
            _windows_app_cli.cache_clear()
            installed = _windows_app_cli()
        return _launcher(installed)
    candidates = [shutil.which("codex"), Path.home() / ".local/bin/codex"]
    if sys.platform == "darwin":
        candidates.extend((Path("/opt/homebrew/bin/codex"), Path("/usr/local/bin/codex"),
                           Path("/Applications/Codex.app/Contents/Resources/codex")))
    return next((found for path in candidates if (found := _launcher(path))), None)


def failure(stderr):
    message = stderr.lower()
    if any(x in message for x in (b"unexpected argument", b"unrecognized subcommand", b"unknown feature")):
        return "当前 Codex CLI 缺少工作台使用的命令或参数。请更新服务账户使用的 CLI。"
    if any(x in message for x in (b"401 unauthorized", b"not logged in", b"reauthentication", b"invalid authentication")):
        return "Codex 登录已失效，请用运行 FastAPI 的账户执行 codex login 后重试。"
    if any(x in message for x in (b"usage_limit_reached", b"429 too many requests", b"quota exceeded")):
        return "Codex 额度或速率受限，请稍后重试。"
    return "Codex CLI 执行失败，请检查服务账户的 codex login status、CLI 版本及网络连接。"


async def generate(tool, instructions, prompt, schema_document, *, model=None, reasoning_effort=None):
    with TemporaryDirectory() as workspace:
        folder = Path(workspace)
        schema = folder / "response-schema.json"
        output = folder / "answer.json"
        schema.write_text(json.dumps(schema_document), encoding="utf-8")
        selection = []
        if model is not None:
            selection.extend(["--model", model])
        if reasoning_effort is not None:
            selection.extend(["-c", "model_reasoning_effort=" + json.dumps(reasoning_effort)])
        args = [*tool, "exec", *selection, "--ignore-user-config", "--skip-git-repo-check", "--ephemeral",
                "--sandbox", "read-only", "--disable", "shell_tool", "--disable", "apps", "--disable", "plugins",
                "-c", "developer_instructions=" + json.dumps(instructions),
                "--color", "never", "--output-schema", str(schema), "-o", str(output), "-"]
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        try:
            process = subprocess.Popen(args, cwd=folder, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.PIPE, **options)
        except OSError as exc:
            raise ValueError("无法启动 Codex CLI，请检查 EFFECT_CODEX 和执行权限。") from exc
        response = asyncio.create_task(asyncio.to_thread(process.communicate, prompt.encode("utf-8")))
        try:
            async with asyncio.timeout(300):
                _, stderr = await asyncio.shield(response)
                if process.returncode:
                    raise ValueError(failure(stderr))
                return json.loads(output.read_text(encoding="utf-8"))
        except TimeoutError as exc:
            raise ValueError("Codex 生成超时，请稍后重试。") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Codex 未返回有效的 JSON。") from exc
        finally:
            with CancelScope(shield=True):
                await stop_process(process)
                await asyncio.gather(response, return_exceptions=True)


async def draft_response(request, generation):
    task = asyncio.create_task(generation)
    try:
        yield b"\n"
        while not task.done():
            if await request.is_disconnected():
                return
            done, _ = await asyncio.wait([task], timeout=2)
            if not done:
                yield b"\n"
        yield json.dumps(task.result(), ensure_ascii=False).encode()
    except ValueError as exc:
        yield json.dumps({"error": str(exc)}, ensure_ascii=False).encode()
    finally:
        task.cancel()
        with CancelScope(shield=True):
            await asyncio.gather(task, return_exceptions=True)
