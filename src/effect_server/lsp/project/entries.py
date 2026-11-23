"""Locate SDK workflows from their handwritten main and its imports."""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from threading import Lock
import time

from tree_sitter import Language, Parser
import tree_sitter_haskell

from .hls import definitions
from .model import constructor_name

HASKELL = Language(tree_sitter_haskell.language())
IGNORED = {"sdk", "Generated", "dist-newstyle", "node_modules", "dist"}


@dataclass(frozen=True)
class Entry:
    namespace: str
    main: Path
    main_module: str
    source_dir: Path  # relative to the Cabal project


def module_name(tree):
    header = next((n for n in tree.named_children if n.type == "header"), None)
    name = header.child_by_field_name("module") if header else None
    return name.text.decode() if name else "Main"


def arguments(expr):
    if expr is None:
        return []
    if expr.type == "apply":
        return arguments(expr.child_by_field_name("function")) + arguments(expr.child_by_field_name("argument"))
    if expr.type == "parens":
        return arguments(expr.child_by_field_name("expression"))
    return [expr]


def entry_use(tree, raw):
    declarations = tree.child_by_field_name("declarations")
    imports = tree.child_by_field_name("imports")
    if tree.has_error or not declarations or not imports:
        return None
    for decl in declarations.named_children:
        name = decl.child_by_field_name("name")
        if decl.type != "bind" or not name or name.text != b"main":
            continue
        match = decl.child_by_field_name("match")
        terms = arguments(match.child_by_field_name("expression")) if match else []
        if [term.text.decode().split(".")[-1] for term in terms] != ["interpreter", "ast", "effects"]:
            continue
        positions = []
        for term in terms:
            token = term.child_by_field_name("id") or term
            before = raw[raw.rfind(b"\n", 0, token.start_byte) + 1:token.start_byte]
            positions.append((token.start_point.row, len(before.decode().encode("utf-16-le")) // 2))
        owners = []
        for term, suffix in zip(terms, (".Generated.Program", ".AST", ".Effects")):
            qualifier, _, symbol = term.text.decode().rpartition(".")
            candidates = set()
            for imp in imports.children_by_field_name("import"):
                module = imp.child_by_field_name("module").text.decode()
                if not module.endswith(suffix):
                    continue
                alias = imp.child_by_field_name("alias")
                flags = {c.type for c in imp.children}
                if qualifier and qualifier != (alias.text.decode() if alias else module):
                    continue
                if not qualifier and "qualified" in flags:
                    continue
                imported = imp.child_by_field_name("names")
                if imported and ((symbol.encode() in {n.text for n in imported.named_children}) == ("hiding" in flags)):
                    continue
                candidates.add(module.removesuffix(suffix))
            if len(candidates) != 1:
                return None, tuple(positions)
            owners.extend(candidates)
        if len(set(owners)) == 1 and all(map(constructor_name, owners[0].split("."))):
            return owners[0], tuple(positions)
        return None, tuple(positions)
    return None


@lru_cache(maxsize=2048)
def module_facts(path: Path, timestamp: int, size: int):
    raw = path.read_bytes()
    if path.name not in {"AST.hs", "Effects.hs"} and (b"main" not in raw or b"interpreter" not in raw):
        return None, None
    tree = Parser(HASKELL).parse(raw).root_node
    return module_name(tree), entry_use(tree, raw)


def entries(root: Path, *, versions=None) -> dict[str, Entry]:
    mains, modules = [], {}
    if versions is None:
        versions = []
    for directory, dirs, files in root.walk():
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in IGNORED
                         and (directory / d).resolve() == directory / d)
        for filename in sorted(files):
            path = directory / filename
            if path.suffix != ".hs" and path.suffix != ".cabal" and filename not in {"hie.yaml", "cabal.project"}:
                continue
            if path.resolve() != path or not path.is_file():
                continue
            stat = path.stat()
            versions.append((path, stat.st_mtime_ns, stat.st_size))
            if path.suffix != ".hs":
                continue
            module, use = module_facts(path, stat.st_mtime_ns, stat.st_size)
            if module:
                modules[path] = module
            if use:
                mains.append((use, path, module))
    effect_files = {}
    for path, module in modules.items():
        if not module.endswith(".Effects"):
            continue
        ns = module.removesuffix(".Effects")
        suffix = (*ns.split("."), "Effects.hs")
        if path.parts[-len(suffix):] == suffix and modules.get(path.with_name("AST.hs")) == ns + ".AST":
            effect_files.setdefault(ns, []).append(path)

    result = {}
    for (ns, positions), main, name in mains:
        candidates = effect_files.get(ns, [])
        if len(candidates) == 1:
            effects = candidates[0]
        else:
            witnesses = (p.parent / "Generated/Program.hs" for paths in effect_files.values() for p in paths)
            generated = tuple((p, p.stat().st_mtime_ns) for p in witnesses if p.is_file())
            paths = resolve_imports(root, main, positions, (tuple(versions), generated))
            if len(paths) != 3:
                if len(candidates) > 1:
                    raise ValueError(f"HLS 无法确定 {main.name} 引用的 {ns} 位于哪个源码目录。")
                continue
            program, ast, effects = paths
            ns = modules.get(effects, "").removesuffix(".Effects")
            if (effects not in effect_files.get(ns, []) or ast != effects.with_name("AST.hs")
                    or program != effects.parent / "Generated/Program.hs"):
                continue
        if ns in result:
            raise ValueError(f"{ns} 对应多个工作流入口，无法唯一定位。")
        source_dir = effects.parents[len(ns.split("."))].relative_to(root)
        result[ns] = Entry(ns, main.relative_to(root), name, source_dir)
    return result


@lru_cache(maxsize=32)
def hls_lookup(main):
    return Lock(), {}


def resolve_imports(root, main, positions, revision):
    lock, cache = hls_lookup(main)
    with lock:
        if cache.get("revision") != revision or time.monotonic() >= cache["expires"]:
            paths = definitions(root, main, positions)
            cache.update(revision=revision, paths=paths, expires=float("inf") if len(paths) == 3 else time.monotonic() + 30)
        return cache["paths"]


def entry_for(root: Path, namespace: str) -> Entry:
    found = entries(root).get(namespace)
    if not found:
        raise FileNotFoundError(f"未找到 {namespace} 的工作流 main。")
    return found


def business_dir(root: Path, value: str) -> Path:
    path = Path(value)
    if not value.strip() or path.anchor or ".." in path.parts or any(
        p in IGNORED or p.startswith(".") for p in path.parts
    ) or (root / path).resolve() != root / path:
        raise ValueError("业务源码目录必须位于项目内，与 SDK 和构建目录分开。")
    return path
