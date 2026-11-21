"""Synchronous SDK transport for local template tools."""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Framework:
    executable: Path
    root: Path
    source_dir: Path = Path("demo")

    def command(self, *arguments: str):
        directory = self.source_dir.as_posix()
        prefix = ["--source-dir", directory] if directory != "demo" else []
        return [str(self.executable.resolve()), *prefix, *arguments]

    def run(self, *arguments: str, source: str = "") -> bytes:
        result = subprocess.run(
            self.command(*arguments),
            cwd=self.root, input=source.encode("utf-8"), capture_output=True,
        )
        if result.returncode:
            raise ValueError(result.stderr.decode("utf-8", errors="replace").strip()
                             or f"effect-codegen exited with {result.returncode}")
        return result.stdout

    def read_json(self, *arguments: str, source: str = "") -> dict:
        return json.loads(self.run(*arguments, source=source).decode("utf-8"))
