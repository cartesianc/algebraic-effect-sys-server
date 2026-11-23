"""Verified multi-file edits of handwritten assets, handlers and workflow branches."""

from hashlib import sha256
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
from typing import Literal

from pydantic import BaseModel

from ..analysis.facade import list_entries, method_definitions, method_nodes
from ..analysis.read import read_namespace
from ..analysis.sources import modules, namespace_folder
from ..framework import Framework
from ..project.entries import IGNORED
from ..project.model import constructor_name, validate_effect
from ..runtime.snapshot import RegistrationConflict, atomic_write, namespace_lock
from .effect import generate_effect, literal
from .registration import append_expression
from .workflow import export_name, insert_import, targets, tree, variable, workflow_edit


class Asset(BaseModel):
    kind: Literal["asset"]
    contract: str
    representation: str
    fields: list[str] = []
    producer: str
    input: str = "Root"
    parser: str
    body: str
    readable: bool = True
    stream: bool = False
    requester: str | None = None


class Handler(BaseModel):
    kind: Literal["handler"]
    effect: str
    role: Literal["handler", "open", "close", "parser", "detect", "resolve"] = "handler"
    name: str
    body: str
    new_effect: bool = False
    replace_body: bool = False
    input: str = "Root"


class WorkflowEdit(BaseModel):
    kind: Literal["workflow"]
    target: str
    action: Literal["append", "append_branch", "name"] = "append"
    effects: list[str] = []
    composition: Literal["chain", "parallel", "alternative", "lifelong"] = "chain"
    name: str = ""
    separate: bool = False


class Registration(BaseModel):
    kind: Literal["registration"]
    registration: dict
    handlers: dict[str, str]


def source_files(folder):
    return {name: path.read_bytes() for name, path in modules(folder, False).items()}


def fingerprint(files):
    digest = sha256()
    for name, raw in sorted(files.items()):
        digest.update(name.encode() + b"\0" + sha256(raw).digest())
    return digest.hexdigest()


def representations(raw):
    block = tree(raw).child_by_field_name("declarations")
    return [d.child_by_field_name("name").text.decode() for d in block.named_children
            if d.type in {"newtype", "data_type"}] if block else []


def editor(namespace, framework):
    files = source_files(namespace_folder(framework.root, namespace, framework.source_dir))
    rows = read_namespace(namespace, framework)["registrations"]
    effects = [row["effect"] for row in rows]
    names = {v for e in effects for k, v in e["typing"].items()
             if k in {"handler", "open", "close", "parser", "detect", "resolve"} and v}
    implemented = method_definitions(files.get("Handlers.hs", b""), names)
    return {"revision": fingerprint(files), "effects": effects, "implemented": [n for n in names if implemented[n]],
            "types": ["String", "[String]", *representations(files.get("Types.hs", b""))],
            "branches": [{"id": key, "kind": op} for key, (_, _, _, op) in targets(files)]}


def constructor(value):
    if not constructor_name(value):
        raise ValueError("资产、类型及 Effect 名须以大写字母开头。")
    return value


def add_instance(raw, ns, method, body):
    variable(method)
    if not body.strip():
        raise ValueError("请输入 Handler 方法体。")
    cls = method[:1].upper() + method[1:]
    if method_definitions(raw, {method})[method]:
        raise RegistrationConflict(f"{method} 已有实现。")
    if not raw:
        raw = f"module {ns}.Handlers () where\n".encode()
    for imp in (f"import {ns}.Generated.Effects", "import Framework.SDK", f"import {ns}.Types"):
        raw = insert_import(raw, imp)
    extensions = ["FlexibleContexts", "FlexibleInstances", "UndecidableInstances", "TypeApplications"]
    pragmas = b" ".join(re.findall(rb"\{-#\s*LANGUAGE\s+([^#]+)#-\}", raw))
    missing = [ext for ext in extensions if not re.search(rb"\b" + ext.encode() + rb"\b", pragmas)]
    if missing:
        raw = ("{-# LANGUAGE " + ", ".join(missing) + " #-}\n").encode() + raw
    expr = "\n".join("    " + line for line in body.strip().splitlines())
    return raw.rstrip() + f"\n\ninstance {cls}Layer m => {cls}Handler m where\n  {method} =\n{expr}\n".encode()


def record_field(record, field):
    return next((f.child_by_field_name("expression") for f in record.children_by_field_name("field")
                 if f.child_by_field_name("field").text.decode() == field), None) if record else None


def replace_body(raw, method, body):
    matches = [node for _, node in method_nodes(raw, {method})]
    if len(matches) != 1 or not body.strip():
        raise ValueError("需要唯一的 Handler 实现和非空方法体。")
    node = matches[0]
    clauses = node.children_by_field_name("match")
    if len(clauses) != 1 or clauses[0].child_by_field_name("guards"):
        raise ValueError("含守卫或多个等式的实现请在手写源码页编辑。")
    expr = clauses[0].child_by_field_name("expression")
    if expr is None:
        raise ValueError("找不到原方法体。")
    indent = expr.start_point.column if expr.start_point.row != node.start_point.row else node.start_point.column + 2
    replacement = ("\n" + " " * indent).join(body.strip().splitlines())
    return raw[:expr.start_byte] + replacement.encode() + raw[expr.end_byte:]


def edits(spec, ns, files, framework):
    rows = read_namespace(ns, framework)["registrations"]
    effects = {r["effect"]["name"]: r["effect"] for r in rows}
    if isinstance(spec, WorkflowEdit):
        return workflow_edit(spec, ns, files, {n for n, e in effects.items() if e["typing"]["kind"] in {"Excu", "Scoped"}})
    raw = files["Effects.hs"]
    if isinstance(spec, Handler) and spec.replace_body:
        if spec.new_effect or effects.get(spec.effect, {}).get("typing", {}).get(spec.role) != spec.name:
            raise RegistrationConflict("Handler 绑定已改变，请重新生成草稿。")
        return {"Handlers.hs": replace_body(files.get("Handlers.hs", b""), spec.name, spec.body)}
    changes = {}
    if isinstance(spec, Registration):
        effect = validate_effect(spec.registration)
        constructor(effect["name"])
        if effect["name"] in {"Root", "None", *effects}:
            raise RegistrationConflict("Effect 名称已存在或保留。")
        roles = {"handler", "parser", "open", "close", "detect", "resolve"}
        bindings = {v for k, v in effect["typing"].items() if k in roles and v}
        existing = {v for e in effects.values() for k, v in e["typing"].items() if k in roles and v}
        if not spec.handlers or set(spec.handlers) != bindings - existing:
            raise ValueError("每个新方法须绑定到本注册的角色并提供唯一实现。已有方法请选择复用。")
        expression = generate_effect(spec.registration, framework)
        registration = append_expression(raw, framework.read_json("registration-slot", ns), expression)
        registration = insert_import(registration, "import Framework.SDK")
        if effect["typing"]["kind"] == "Context":
            registration = insert_import(registration, f"import {ns}.Types")
            registration = b"{-# LANGUAGE TypeApplications #-}\n" + registration
        handlers = files.get("Handlers.hs", b"")
        for name, body in spec.handlers.items():
            handlers = add_instance(handlers, ns, name, body)
        return {"Effects.hs": registration, "Handlers.hs": handlers}
    if isinstance(spec, Asset):
        constructor(spec.contract)
        if any(e["typing"].get("asset", {}).get("contract") == spec.contract for e in effects.values()):
            raise RegistrationConflict("资产契约已存在。")
        representation = spec.representation
        types = files.get("Types.hs", f"module {ns}.Types where\n".encode())
        known = {"String", "[String]", *representations(types)}
        if representation not in known:
            constructor(representation)
            if not spec.fields or any(f not in known for f in spec.fields):
                raise ValueError("新表示类型至少需要一个字段，字段须为已有表示类型。")
            keyword = "newtype" if len(spec.fields) == 1 else "data"
            definition = f"\n\n{keyword} {representation} = {representation} {' '.join(spec.fields)} deriving (Eq, Show)\n"
            changes["Types.hs"] = export_name(types, representation + "(..)").rstrip() + definition.encode()
        elif spec.fields:
            raise ValueError("复用已有表示类型时无需声明字段。")
        name, method = spec.producer, spec.parser
        typing = {"kind": "Context", "asset": {"type": representation, "contract": spec.contract},
                  "parser": method, "require": spec.readable, "stream": spec.stream}
    else:
        name, method = spec.effect, spec.name
        typing = {"kind": "Excu", "handler": method}
    if isinstance(spec, Asset) or spec.new_effect:
        constructor(name)
        if name in effects or name in {"Root", "None"}:
            raise RegistrationConflict("Effect 名称已存在或保留。")
        if spec.input not in {"Root", *effects}:
            raise ValueError("找不到所选的上游 Effect。")
        expression = generate_effect({"schemaVersion": 1, "effect": {
            "name": name, "input": spec.input, "typing": typing, "demands": []}}, framework)
        raw = append_expression(raw, framework.read_json("registration-slot", ns), expression)
        if isinstance(spec, Asset) and spec.requester:
            entries = list_entries(raw, len(rows) + 1)
            if not entries or spec.requester not in effects:
                raise ValueError("请选择可编辑的请求方注册。")
            demands = record_field(entries[list(effects).index(spec.requester)], "demands")
            if demands is None or demands.type not in {"list", "constructor_list"} or not demands.text.endswith(b"]"):
                raise ValueError("请求方的 demands 须为显式列表。")
            elements = demands.children_by_field_name("element")
            pos = elements[-1].end_byte if elements else demands.start_byte + 1
            raw = raw[:pos] + ((", " if elements else "") + "requests " + literal(spec.contract)).encode() + raw[pos:]
            raw = insert_import(raw, "import Framework.SDK (requests)")
        raw = insert_import(raw, "import Framework.SDK (EffectSpec(..), EffectTyping(..), Asset(..))")
        if isinstance(spec, Asset):
            raw = insert_import(raw, f"import {ns}.Types")
            if not re.search(rb"\bTypeApplications\b", raw):
                raw = b"{-# LANGUAGE TypeApplications #-}\n" + raw
    else:
        effect = effects.get(name)
        if not effect or spec.role not in effect["typing"]:
            raise ValueError("该 Effect 没有所选角色。")
        previous = effect["typing"][spec.role]
        if previous and previous != method:
            raise RegistrationConflict("此角色已绑定其他方法。")
        if not previous:
            entries = list_entries(raw, len(rows))
            index = list(effects).index(name)
            if not entries:
                raise ValueError("此注册由表达式计算，请在手写源码中绑定方法。")
            slot = record_field(record_field(entries[index], "typing"), spec.role)
            if not slot or slot.text != b"Nothing":
                raise ValueError("此绑定不是显式 Nothing，请在手写源码中编辑。")
            raw = raw[:slot.start_byte] + f'Just "{variable(method)}"'.encode() + raw[slot.end_byte:]
    changes["Handlers.hs"] = add_instance(files.get("Handlers.hs", b""), ns, method, spec.body)
    if raw != files["Effects.hs"]:
        changes["Effects.hs"] = raw
    return changes


def save_edit(ns, spec, expected, framework):
    folder = namespace_folder(framework.root, ns, framework.source_dir)
    with namespace_lock(ns), TemporaryDirectory() as temp:
        original = source_files(folder)
        baseline = fingerprint(original)
        version = "facade:" + sha256(original["Effects.hs"]).hexdigest() if isinstance(spec, Registration) else baseline
        if version != expected:
            raise RegistrationConflict("业务源码已更改，请重新打开编辑窗口。")
        changes = edits(spec, ns, original, framework)
        staged = Path(temp) / "src"
        ignored = shutil.ignore_patterns(".*", *sorted(IGNORED - {"Generated"}))
        shutil.copytree(framework.root / framework.source_dir, staged,
                        ignore=lambda folder, names: ignored(folder, names) | {
                            n for n in names if (Path(folder) / n).resolve() != Path(folder) / n})
        work = staged.joinpath(*ns.split("."))
        writes = {}
        for file, raw in changes.items():
            path = folder / file
            if path.resolve() != path or (path.exists() and file not in original):
                raise RegistrationConflict("目标路径不可写或文件已存在。")
            if b"\r\n" in original.get(file, b""):
                raw = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            dest = work / file
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
            writes[path] = (original.get(file), raw)
        # Keep Cabal's real package environment; only business sources and outputs move.
        candidate = Framework(framework.executable, framework.root, staged)
        candidate.run("compiler", ns)
        if fingerprint(source_files(folder)) != baseline:
            raise RegistrationConflict("验证期间源码已更改。")
        for file in changes:
            if file.startswith("Workflow/") and file not in original:
                module = ns + "." + file[:-3].replace("/", ".")
                for manifest in framework.root.glob("*.cabal"):
                    if manifest.resolve() != manifest:
                        continue
                    before = manifest.read_bytes()
                    pattern = rb"(?m)^([ \t]*)" + re.escape(ns.encode()) + rb"\.AST([ \t]*\r?)$"
                    after = re.sub(pattern, lambda m: m[0] + b"\n" + m[1] + module.encode() + m[2], before)
                    if after != before and module.encode() not in before.split():
                        writes[manifest] = (before, after)
        applied = []
        try:
            for path, (before, after) in writes.items():
                if (path.read_bytes() if path.exists() else None) != before:
                    raise RegistrationConflict(f"{path.name} 已更改。")
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(path, after)
                applied.append(path)
        except Exception:
            for path in reversed(applied):
                before, after = writes[path]
                if path.read_bytes() == after:
                    if before is None:
                        path.unlink()
                    else:
                        atomic_write(path, before)
            raise
        return {"files": list(changes), "revision": fingerprint(source_files(folder))}
