"""Independent inputs for the Facade editor and the generated-module reviewer."""

from hashlib import sha256
from pathlib import Path

from tree_sitter import Language, Parser
import tree_sitter_haskell

from ..project.model import constructor_name
from ..runtime.snapshot import RegistrationConflict, atomic_write, namespace_lock


HASKELL = Language(tree_sitter_haskell.language())


def namespace_folder(root: Path, namespace: str) -> Path:
    if not all(map(constructor_name, namespace.split("."))):
        raise ValueError("无效的命名空间。")
    folder = root.joinpath("demo", *namespace.split("."))
    if folder.resolve() != folder or not folder.is_dir():
        raise FileNotFoundError(namespace)
    return folder


def source_namespaces(root: Path):
    base = root / "demo"
    for path in base.rglob("Effects.hs"):
        folder = path.parent.parent if path.parent.name == "Generated" else path.parent
        parts = folder.relative_to(base).parts
        if parts and all(map(constructor_name, parts)) and path.resolve() == path and path.is_file():
            yield ".".join(parts)


def modules(folder: Path, generated: bool) -> dict[str, Path]:
    base = folder / "Generated" if generated else folder
    if base.resolve() != base:
        raise ValueError("模块目录超出命名空间。")
    result = {}
    for path in sorted(base.rglob("*.hs")):
        relative = path.relative_to(base)
        if not generated and "Generated" in relative.parts:
            continue
        # Nested namespace modules belong to their own editor.
        if not generated and any((p / "Effects.hs").exists() for p in path.parents
                                 if p != base and p.is_relative_to(base)):
            continue
        if path.resolve() == path and path.is_file():
            result[relative.as_posix()] = path
    return result


def revision(data: bytes) -> str:
    return sha256(data).hexdigest()


def inspect_haskell(raw: bytes) -> dict:
    tree = Parser(HASKELL).parse(raw)
    root = tree.root_node
    definitions = []
    bodies = []
    errors = []

    def visit(node):
        if node.is_error or node.is_missing:
            errors.append({"line": node.start_point.row + 1, "column": node.start_point.column + 1,
                           "message": f"缺少 {node.type}" if node.is_missing else "Haskell 语法错误"})
        elif node.has_error:
            for child in node.children:
                if child.has_error:
                    visit(child)

    visit(root)
    header = next((n for n in root.named_children if n.type == "header"), None)
    module = header.child_by_field_name("module") if header else None

    def declarations(block):
        if not block:
            return
        for decl in block.named_children:
            name = decl.child_by_field_name("name")
            defines_name = decl.type not in ("signature", "instance", "deriving_instance")
            if decl.type == "signature" and block.type == "class_declarations":
                defines_name = True
            if name and defines_name:
                definitions.append({"name": name.text.decode(), "kind": decl.type,
                                    "line": decl.start_point.row + 1, "endLine": decl.end_point.row + 1})
                bodies.append(decl.child_by_field_name("match"))
            if decl.type in ("instance", "class"):
                declarations(decl.child_by_field_name("declarations"))

    declarations(root.child_by_field_name("declarations"))
    names = {d["name"] for d in definitions}
    for definition, body in zip(definitions, bodies):
        references = []
        pending = [body] if body else []
        while pending:
            node = pending.pop()
            if node.type == "variable":
                name = node.text.decode()
                if name in names and name not in references:
                    references.append(name)
            elif node.type != "qualified":
                pending.extend(reversed(node.named_children))
        definition["references"] = references
    return {"module": module.text.decode() if module else None, "definitions": definitions, "diagnostics": errors}


def source_document(folder: Path, generated: bool, file: str | None) -> dict:
    files = modules(folder, generated)
    default = "Handlers.hs" if generated else "Effects.hs"
    if file is None:
        file = default if default in files else next(iter(files), None)
    if file is None:
        return {"files": [], "file": None}
    if file not in files:
        raise FileNotFoundError(file)
    raw = files[file].read_bytes()
    parsed = inspect_haskell(raw)
    return {"files": list(files), "file": file, "source": raw.decode("utf-8"),
            "revision": revision(raw), **parsed}


def save_facade(folder: Path, namespace: str, file: str, source: str, expected: str) -> dict:
    with namespace_lock(namespace):
        target = modules(folder, False).get(file)
        if not target:
            raise FileNotFoundError(file)
        original = target.read_bytes()
        if revision(original) != expected:
            raise RegistrationConflict("磁盘上的文件已修改，请保留草稿并重新读取。")
        content = source.replace("\r\n", "\n").encode("utf-8")
        if b"\r\n" in original:
            content = content.replace(b"\n", b"\r\n")
        atomic_write(target, content)
        return {"revision": revision(content)}

