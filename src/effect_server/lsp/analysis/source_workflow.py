"""Follow the handwritten ast binding, retaining the source target of each editable branch."""

from ..codegen.workflow import bindings, combination, unparen, workflow_modules


def source_workflow(files):
    roots = dict(workflow_modules(files))
    bodies = {(file, name): body for file, root in roots.items() for name, body in bindings(root)}
    if ("AST.hs", "ast") not in bodies:
        raise ValueError("AST.hs 中找不到 ast 绑定。")
    modules = {}
    imports = {}
    for file, root in roots.items():
        header = next((n for n in root.named_children if n.type == "header"), None)
        if header:
            modules[header.child_by_field_name("module").text.decode()] = file
        block = root.child_by_field_name("imports")
        imports[file] = block.children_by_field_name("import") if block else []

    def owners(file, symbol):
        prefix, _, name = symbol.rpartition(".")
        if not prefix and (file, name) in bodies:
            return {file}
        found = set()
        for imp in imports[file]:
            mod = imp.child_by_field_name("module").text.decode()
            alias = imp.child_by_field_name("alias")
            flags = {child.type for child in imp.children}
            if prefix and prefix != (alias.text.decode() if alias else mod):
                continue
            if not prefix and "qualified" in flags:
                continue
            names = imp.child_by_field_name("names")
            if names and ((name in {n.text.decode() for n in names.named_children}) == ("hiding" in flags)):
                continue
            if mod in {"Framework.SDK", "Framework.Language.AST"} or (modules.get(mod), name) in bodies:
                found.add(modules.get(mod, mod))
        return found

    def render(file, name, node, target, stack):
        node = unparen(node)
        expression = node.text.decode()
        if node.type in {"variable", "qualified"}:
            symbol = expression.rsplit(".", 1)[-1]
            candidates = [(owner, symbol) for owner in owners(file, expression) if (owner, symbol) in bodies]
            if len(candidates) == 1 and candidates[0] not in stack:
                return branch(candidates[0], stack)
        if node.type == "apply":
            symbol = node.child_by_field_name("function").text.decode()
            if owners(file, symbol) in ({"Framework.SDK"}, {"Framework.Language.AST"}):
                op, args = combination(node)
                if op:
                    return {"kind": op.title(), "target": target, "name": name, "children": [
                        render(file, None, child, f"{target}/{i}", stack)
                        for i, child in enumerate(args.children_by_field_name("element"))]}
                arg = node.child_by_field_name("argument")
                if symbol.rsplit(".", 1)[-1] == "node" and arg.type == "literal" and arg.named_children[0].type == "string":
                    return {"kind": "Node", "effect": arg.text.decode()[1:-1], "children": []}
        return {"kind": "Node", "expression": expression, "children": []}

    def branch(key, stack):
        file, name = key
        return render(file, name, bodies[key], f"{file}:{name}", stack | {key})

    return branch(("AST.hs", "ast"), set())
