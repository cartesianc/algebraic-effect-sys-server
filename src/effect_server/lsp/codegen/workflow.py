"""Edit named AST bindings and explicit SDK branch lists without flattening the workflow."""

import re

from tree_sitter import Parser

from ..project.entries import HASKELL
from .effect import literal


COMBINATORS = {"chain", "parallel", "alternative", "lifelong"}


def tree(source: bytes):
    root = Parser(HASKELL).parse(source).root_node
    if root.has_error:
        raise ValueError("模块有 Haskell 语法错误，请先修正源码。")
    return root


def insert_import(source: bytes, statement: str) -> bytes:
    root = tree(source)
    imports = root.child_by_field_name("imports")
    entries = imports.children_by_field_name("import") if imports else []
    if any(node.text.decode() == statement for node in entries):
        return source
    header = next(n for n in root.named_children if n.type == "header")
    end = entries[-1].end_byte if entries else header.end_byte
    # Insert after the full line so a trailing comment stays with its declaration.
    line = source.find(b"\n", end)
    if line < 0:
        return source + b"\n" + statement.encode() + b"\n"
    return source[:line+1] + statement.encode() + b"\n" + source[line+1:]


def export_name(source: bytes, name: str) -> bytes:
    header = next(n for n in tree(source).named_children if n.type == "header")
    exports = header.child_by_field_name("exports")
    if not exports:
        return source
    entries = exports.children_by_field_name("export")
    if any(e.text.decode() == name for e in entries):
        return source
    pos = entries[-1].end_byte if entries else exports.start_byte + 1
    return source[:pos] + ((", " if entries else "") + name).encode() + source[pos:]


def variable(name: str):
    if not re.fullmatch(r"[a-z][a-zA-Z0-9_']*", name):
        raise ValueError("绑定名须以小写英文字母开头。")
    return name


def combination(node):
    if node.type == "apply":
        op = node.child_by_field_name("function").text.decode().rsplit(".", 1)[-1]
        args = node.child_by_field_name("argument")
        if op in COMBINATORS and args.type == "list":
            return op, args
    return None, None


def unparen(node):
    while node.type == "parens":
        node = node.child_by_field_name("expression")
    return node


def workflow_modules(sources):
    for file, raw in sources.items():
        if file == "AST.hs" or file.startswith("Workflow/"):
            yield file, tree(raw)


def bindings(root):
    declarations = root.child_by_field_name("declarations")
    for decl in declarations.named_children if declarations else ():
        name = decl.child_by_field_name("name")
        match = decl.child_by_field_name("match")
        if decl.type == "bind" and name and match:
            body = match.child_by_field_name("expression")
            if body:
                yield name.text.decode(), unparen(body)


def targets(sources):
    def walk(file, binding, node, path):
        op, args = combination(node)
        key = f"{file}:{binding}" + "".join(f"/{i}" for i in path)
        yield key, (file, binding, node, op)
        if args:
            for i, child in enumerate(args.children_by_field_name("element")):
                child = unparen(child)
                if combination(child)[0]:
                    yield from walk(file, binding, child, [*path, i])

    for file, root in workflow_modules(sources):
        for name, body in bindings(root):
            yield from walk(file, name, body, [])


def workflow_edit(spec, namespace, sources, effect_names):
    target = dict(targets(sources)).get(spec.target)
    if not target:
        raise ValueError("找不到选定的 Workflow 分支，请刷新。")
    file, _, node, _ = target
    raw = sources[file]
    old = node.text.decode()
    if spec.action != "name":
        if not spec.effects or any(e not in effect_names for e in spec.effects):
            raise ValueError("请选择至少一个已注册的 Workflow 入口。")
        children = ["node " + literal(e) for e in spec.effects]
        branch = spec.composition + " [" + ", ".join(children) + "]"
    else:
        branch = old
    added = {}
    branch_name = spec.name.strip()
    if (spec.action == "name" or spec.separate) and not branch_name:
        raise ValueError("请输入分支名称。")
    reference = branch
    imports = []
    declarations = ""
    if branch_name:
        variable(branch_name)
        if spec.separate:
            suffix = branch_name[:1].upper() + branch_name[1:]
            module = namespace + ".Workflow." + suffix
            filename = "Workflow/" + suffix + ".hs"
            if filename in sources:
                raise ValueError("同名 Workflow 模块已存在。")
            original_imports = tree(raw).child_by_field_name("imports")
            needed = [n.text.decode() for n in original_imports.children_by_field_name("import")] if original_imports and spec.action == "name" else []
            needed.append("import Framework.SDK (AST, chain, parallel, alternative, lifelong, node)")
            added[filename] = (f"module {module} ({branch_name}) where\n\n" + "\n".join(dict.fromkeys(needed))
                               + f"\n\n{branch_name} :: AST\n{branch_name} = {branch}\n").encode()
            imports.append("import qualified " + module)
            reference = module + "." + branch_name
        else:
            if any(n.child_by_field_name("name") and n.child_by_field_name("name").text.decode() == branch_name
                   for n in tree(raw).child_by_field_name("declarations").named_children):
                raise ValueError("当前模块已有同名定义。")
            declarations = f"\n{branch_name} :: AST\n{branch_name} = {branch}\n"
            reference = branch_name
    if spec.action == "name":
        replacement = reference
    else:
        _, args = combination(node)
        if args is None:
            replacement = f"chain [{old}, {reference}]"
        else:
            elements = args.children_by_field_name("element")
            has_nodes = any(e.type == "apply" and e.child_by_field_name("function").text.decode().rsplit(".", 1)[-1] == "node"
                            for e in map(unparen, elements))
            plain = spec.action == "append" and len(children) == 1 and not branch_name and spec.composition == "chain"
            addition = children[0] if plain and (has_nodes or not elements) else reference
            # SDK lists are homogeneous. Lift existing leaves when inserting a branch.
            inside = node.text
            pos = (elements[-1].end_byte if elements else args.start_byte + 1) - node.start_byte
            inside = inside[:pos] + ((", " if elements else "") + addition).encode() + inside[pos:]
            if has_nodes and not plain:
                for e in reversed(elements):
                    a, b = e.start_byte-node.start_byte, e.end_byte-node.start_byte
                    inside = inside[:a] + b"chain [" + inside[a:b] + b"]" + inside[b:]
            replacement = inside.decode()
    updated = raw[:node.start_byte] + replacement.encode() + raw[node.end_byte:] + declarations.encode()
    imports.append("import Framework.SDK (AST, chain, parallel, alternative, lifelong, node)")
    for imp in imports:
        updated = insert_import(updated, imp)
    if branch_name and not spec.separate:
        updated = export_name(updated, branch_name)
    return {file: updated, **added}
