"""Synchronous SDK transport for local template tools."""

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Framework:
    executable: Path
    root: Path
    source_dir: Path = Path("demo")

    def environment(self):
        env = os.environ.copy()
        # Cabal's persistent environment uses a relative local package-db, including
        # on Windows where cabal exec can corrupt a non-ASCII absolute project path.
        files = list(self.root.glob(".ghc.environment.*"))
        if len(files) == 1 and files[0].is_file():
            env.setdefault("GHC_ENVIRONMENT", str(files[0]))
        return env

    def command(self, *arguments: str):
        directory = self.source_dir.as_posix()
        prefix = ["--source-dir", directory] if directory != "demo" else []
        return [str(self.executable.resolve()), *prefix, *arguments]

    def run(self, *arguments: str, source: str = "") -> bytes:
        result = subprocess.run(
            self.command(*arguments),
            cwd=self.root, input=source.encode("utf-8"), capture_output=True, env=self.environment(),
        )
        if result.returncode:
            raise ValueError(result.stderr.decode("utf-8", errors="replace").strip()
                             or f"effect-codegen exited with {result.returncode}")
        return result.stdout

    def read_json(self, *arguments: str, source: str = "") -> dict:
        return json.loads(self.run(*arguments, source=source).decode("utf-8"))
