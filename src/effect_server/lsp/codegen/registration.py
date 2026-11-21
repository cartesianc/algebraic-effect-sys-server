"""Append one registration and publish the SDK analysis of the edited source."""

import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from ..analysis.sources import modules, namespace_folder, revision as source_revision
from ..analysis.read import read_namespace
from ..framework import Framework
from ..project.model import TYPING_FIELDS, constructor_name, validate_effect
from ..runtime.snapshot import RegistrationConflict, atomic_write, directory, namespace_lock, stamp, store
from .effect import generate_effect


def append_expression(original: bytes, slot: dict, expression: str) -> bytes:
    lines = original.splitlines(keepends=True)
    row = slot["line"] - 1
    line = lines[row].decode("utf-8")
    index = slot["index"]
    if line[index:index + 1] != "]":
        raise RegistrationConflict("无法定位注册列表末尾，请重新读取源码。")
    indent = line[:len(line) - len(line.lstrip(" \t"))]
    eol = "\r\n" if line.endswith("\r\n") or (not line.endswith("\n") and b"\r\n" in original) else "\n"
    # Keep comments and the rest of the closing line, including any local where clause.
    head = line[:index]
    prefix = head + eol if head.strip() else ""
    if prefix:
        indent += "  "
    separator = "" if slot["empty"] else ", "
    item = (eol + indent + "  ").join(expression.splitlines())
    lines[row] = (prefix + indent + separator + item + eol + indent + line[index:]).encode("utf-8")
    return b"".join(lines)


def add_registration(namespace: str, revision: str, document: dict, framework: Framework) -> dict:
    if not all(constructor_name(part) for part in namespace.split(".")):
        raise ValueError("无效的命名空间。")
    effect = validate_effect(document)
    name = effect["name"]
    if not constructor_name(name) or name in {"Root", "None"}:
        raise ValueError("Effect 名称须为大写开头的标识符，Root 和 None 为保留名。")
    relative = Path("demo", *namespace.split("."), "Effects.hs")
    target = modules(namespace_folder(framework.root, namespace), False).get("Effects.hs")
    if not target:
        raise ValueError("找不到可编辑的 Effects.hs。")
    with namespace_lock(namespace):
        baseline = target.read_bytes()
        if revision.startswith("facade:"):
            if revision != "facade:" + source_revision(baseline):
                raise RegistrationConflict("Effects.hs 已修改，请刷新声明。")
            published = read_namespace(namespace, framework)
            handlers = {v for row in published["registrations"]
                        for k, v in row["effect"]["typing"].items()
                        if k in {"handler", "parser", "detect", "resolve", "open", "close"} and v}
        else:
            published = json.loads((directory() / (namespace + ".json")).read_bytes())
            if published["analyzedAt"] != revision:
                raise RegistrationConflict("分析快照已更新，请刷新后重试。")
            handlers = {b["handler"] for b in published["bindings"]}
        names = {row["effect"]["name"] for row in published["registrations"]}
        if name in names:
            raise RegistrationConflict(f"{name} 已注册。")
        if effect["input"] not in names | {"Root", "None"}:
            raise ValueError("请选择已有的 Input。")
        typing = effect["typing"]
        for field in TYPING_FIELDS[typing["kind"]]:
            if field in {"handler", "parser", "detect", "resolve", "open", "close"}:
                if typing[field] is not None and typing[field] not in handlers:
                    raise ValueError("请选择已注册的 Handler。")
        expression = generate_effect(document, framework)
        with TemporaryDirectory() as temp:
            stage = Path(temp)
            for folder in ("sdk", "demo"):
                shutil.copytree(framework.root / folder, stage / folder)
            candidate_file = stage / relative
            original = candidate_file.read_bytes()
            reader = Framework(framework.executable, stage)
            slot = reader.read_json("registration-slot", namespace)
            candidate = append_expression(original, slot, expression)
            candidate_file.write_bytes(candidate)
            data = stamp(namespace, framework.read_json("console", namespace, "--source-root", str(stage)))
            if sum(row["effect"]["name"] == name for row in data["registrations"]) != 1:
                raise ValueError("SDK 未能读回唯一的新注册。")
            if target.read_bytes() != baseline or original != baseline:
                raise RegistrationConflict("Effects.hs 在分析期间发生了修改，请重试。")
            atomic_write(target, candidate)
            try:
                store(data)
            except OSError:
                if target.read_bytes() == candidate:
                    atomic_write(target, original)
                raise
            return data
