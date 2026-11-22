"""Resolve the three names used by a workflow main against its project cradle."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
from threading import Timer


def hls_tool():
    return shutil.which(os.getenv("EFFECT_HLS") or "haskell-language-server-wrapper")


def write_message(pipe, message):
    body = json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False).encode("utf-8")
    pipe.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    pipe.flush()


def read_message(pipe):
    length = None
    while True:
        header = pipe.readline()
        if not header:
            raise EOFError("HLS 已退出")
        if not header.strip():
            break
        key, _, value = header.partition(b":")
        if key.lower() == b"content-length":
            length = int(value)
    if length is None or length < 1:
        raise ValueError("HLS 返回了无效的数据包。")
    return json.loads(pipe.read(length))


def definitions(root: Path, main: Path, positions):
    tool = hls_tool()
    if not tool:
        return []
    options = ({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True})
    with subprocess.Popen([tool, "--lsp"], cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, **options) as process:
        def stop():
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

        def send(**message):
            write_message(process.stdin, message)

        def call(number, method, params):
            send(id=number, method=method, params=params)
            while True:
                msg = read_message(process.stdout)
                if "method" in msg and "id" in msg:
                    if msg["method"] == "workspace/configuration":
                        send(id=msg["id"], result=[None] * len(msg["params"]["items"]))
                    else:
                        send(id=msg["id"], error={"code": -32601, "message": "Unsupported client request"})
                elif "method" not in msg and msg.get("id") == number:
                    return msg.get("result")

        timer = Timer(65, stop)
        timer.start()
        try:
            uri = main.as_uri()
            call(0, "initialize", {"processId": os.getpid(), "rootUri": root.as_uri(),
                                   "capabilities": {}, "workspaceFolders": [{"uri": root.as_uri(), "name": root.name}]})
            send(method="initialized", params={})
            send(method="textDocument/didOpen", params={"textDocument": {
                "uri": uri, "version": 1, "languageId": "haskell", "text": main.read_text(encoding="utf-8-sig")}})
            paths = []
            for number, (line, column) in enumerate(positions, 1):
                locations = call(number, "textDocument/definition", {"textDocument": {"uri": uri},
                                 "position": {"line": line, "character": column}})
                if not locations:
                    break
                locations = locations if isinstance(locations, list) else [locations]
                files = {Path.from_uri(loc.get("uri") or loc["targetUri"]).resolve() for loc in locations}
                if len(files) != 1 or not next(iter(files)).is_relative_to(root):
                    break
                paths.append(files.pop())
            call(4, "shutdown", None)
            send(method="exit")
            process.stdin.close()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                stop()
            return paths
        except (EOFError, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            return []
        finally:
            timer.cancel()
            stop()
