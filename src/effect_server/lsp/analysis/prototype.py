"""Read the SDK's emitted Haskell value without loading or executing a module."""

import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from tree_sitter import Parser

from .sources import HASKELL


def field(node, name):
    value = node.child_by_field_name(name)
    if value is None:
        raise ValueError(f"缺少字段 {name}")
    return value


def name(node):
    return node.text.decode().rsplit(".", 1)[-1]


def items(node):
    if node.type not in ("list", "tuple"):
        raise ValueError("预期列表或元组")
    return node.children_by_field_name("element")


def record(node):
    if node.type != "record":
        raise ValueError("预期 Haskell 记录")
    return {name(field(f, "field")): field(f, "expression") for f in node.children_by_field_name("field")}


def application(node):
    args = []
    while node.type in ("apply", "parens"):
        if node.type == "parens":
            node = field(node, "expression")
        else:
            args.append(field(node, "argument"))
            node = field(node, "function")
    return name(node), args[::-1]


def string(node):
    text = node.text.decode()
    if not (text.startswith('"') and text.endswith('"')):
        raise ValueError("预期字符串标识符")
    # GHC's Show uses decimal escapes for non-ASCII identifiers and \& as a terminator.
    return re.sub(r'\\(\d+|&|"|\\)', lambda m: chr(int(m[1])) if m[1].isdigit()
                  else "" if m[1] == "&" else m[1], text[1:-1])


def method(node):
    tag = name(node)
    if not tag.endswith("Gen"):
        raise ValueError(f"无法读取方法标识 {tag}")
    return tag[0].lower() + tag[1:-3]


def typing(node):
    kind, args = application(node)
    result = {"kind": kind}
    roles = {"Pure": ("handler",), "Excu": ("handler",), "Stream": ("handler",), "Scoped": ("open", "close"),
             "Middleware": ("handler",), "Error": ("detect", "resolve"), "Context": ("parser",)}
    if kind == "Context":
        _, (contract, representation) = application(args[0])
        ref, type_args = application(representation)
        representation_type = (field(type_args[0], "type").text.decode().removeprefix("AssetTypes.")
                               if ref == "typeRef" else representation.text.decode())
        result["asset"] = {"contract": string(contract), "type": representation_type}
        result.update(require=name(args[2]) == "True", stream=name(args[3]) == "True")
        args = args[1:]
    elif kind == "Middleware":
        result["intercept"] = [name(k) for k in items(args[1])]
    elif kind == "Error":
        result["handles"] = []
        for h in items(args[2]):
            scope, (target,) = application(h)
            result["handles"].append({"kind": scope, "target": string(target)})
    for role, arg in zip(roles[kind], args):
        tag, value = application(arg)
        result[role] = string(value[0]) if tag == "Just" else None
    return result


def workflow(node, selections):
    tag, args = application(node)
    path = [int(n.text) for n in items(args[0])]
    tree = {"kind": tag.removesuffix("At"), "path": path, "children": []}
    if tag == "NodeAt":
        tree["effect"] = string(args[1])
        return tree
    children = [workflow(child, selections) for child in items(args[-1])]
    tree["children"] = children
    if tag == "AlternativeAt":
        plan = record(field(args[1], "expression") if args[1].type == "parens" else args[1])
        tree["selector"] = string(plan["alternativeSource"])
        candidates = {tuple(c["path"]): c["effect"] for c in children if c["kind"] == "Node"}
        for case in items(plan["alternativeCases"]):
            at, provider = items(case)
            selections[candidates[tuple(int(n.text) for n in items(at))]].append((tree["selector"], string(provider)))
    return tree


def consumer_chains(effects, inputs, providers, selected, entry_calls):
    origins = {n for n, e in effects.items() if e["typing"]["kind"] in ("Scoped", "Excu")}
    sources = {}
    for ctx, effect in effects.items():
        if asset := effect["typing"].get("asset"):
            trail = inputs[ctx]
            anchor = next((n for n in trail if n in origins), None)
            steps = trail[:trail.index(anchor)] if anchor else trail
            sources[asset["contract"]] = {"anchor": anchor, "steps": [*reversed(steps), ctx]}

    chains = {}
    for n in effects:
        if n not in origins:
            continue
        parents, branches = {}, []
        visited = set()

        def add_branch(ctx, feeds, via):
            key = (ctx, feeds, via)
            if key in visited:
                return
            visited.add(key)
            asset = effects[ctx]["typing"]["asset"]["contract"]
            source = sources[asset]
            branch = dict(source, context=ctx, asset=asset, feeds=feeds, via=via,
                          session=effects[ctx]["typing"]["stream"])
            anchor = source["anchor"]
            if anchor and not branch["session"]:
                parent = parents.setdefault(anchor, {"effect": anchor, "via": "anchor", "for": []})
                if parent["via"] == "anchor":
                    if asset not in parent["for"]:
                        parent["for"].append(asset)
                    if feeds:
                        parent.setdefault("through", feeds)
            if via == "requests" and not branch["session"]:
                for nested in providers[ctx]:
                    add_branch(nested, ctx, "requests")
            branches.append(branch)

        for selector, ctx in selected[n]:
            parents[selector] = {"effect": selector, "via": "select",
                                 "for": [effects[ctx]["typing"]["asset"]["contract"]]}
            add_branch(ctx, None, "select")
        for parent in entry_calls[n]:
            if parent in inputs[n] and parent in origins:
                parents.setdefault(parent, {"effect": parent, "via": "input"})
        for ctx in providers[n]:
            add_branch(ctx, None, "requests")
        chains[n] = {"effect": n, "kind": effects[n]["typing"]["kind"], "parents": list(parents.values()),
                     "branches": branches}

    def ancestors(n, seen):
        for p in chains[n]["parents"]:
            if p["effect"] not in seen:
                seen.add(p["effect"])
                ancestors(p["effect"], seen)
        return seen

    upstream = {n: ancestors(n, set()) for n in chains}
    for c in chains.values():
        parents = c["parents"]
        c["parents"] = [p for p in parents if not any(p["effect"] in upstream[q["effect"]]
                        for q in parents if p != q)]
    return list(chains.values()), sources


def project(root, namespace):
    decls = field(root, "declarations")
    header = next(n for n in root.named_children if n.type == "header")
    if field(header, "module").text.decode() != namespace + ".Generated.Prototype":
        raise ValueError("模块名与命名空间不符")
    bind = next(n for n in decls.named_children if n.type == "bind" and name(field(n, "name")) == "generatedPrototype")
    value = record(field(field(bind, "match"), "expression"))
    specs = {}
    for row in items(value["protoRegistrations"]):
        spec = record(row)
        specs[string(spec["name"])] = typing(spec["typing"])

    effects, inputs, owners = {}, {}, {}
    calls, request_calls = defaultdict(list), defaultdict(set)
    resolved_requests = defaultdict(list)
    accesses = {"RequireAccess": "requires", "RequestAccess": "requests", "StreamAccess": "streams"}
    for row in items(value["protoEffects"]):
        fields = record(row) if row.type == "record" else {}
        _, (label, kind, origin) = application(field(row, "expression") if fields else row)
        n = string(label)
        if specs[n]["kind"] != name(kind).removesuffix("Kind"):
            raise ValueError(f"{n} 的注册与原型类别不符")
        demands = []
        for d in items(fields["effectDemands"]) if "effectDemands" in fields else ():
            _, (access, contract, provider) = application(d)
            action = accesses[name(access)]
            demands.append({"contract": string(contract), "access": action})
            if action == "requests":
                resolved_requests[n].append(string(provider))
        effects[n] = {"name": n, "typing": specs[n], "input": string(origin), "demands": demands}
        inputs[n] = [string(x) for x in items(fields["effectInputChain"])] if "effectInputChain" in fields else []
        for pair in items(fields["effectMethods"]) if "effectMethods" in fields else ():
            role, ident = items(pair)
            owners[method(ident)] = (n, name(role))

    graph = record(value["protoGraph"])
    reaches = {}
    for row in items(value["protoChainDefinitions"]):
        definition = record(row)
        reaches[method(definition["definitionEntry"])] = [method(m) for m in items(definition["definitionReaches"])]
    references = []
    for pair in items(graph["graphReferences"]):
        caller, callee = map(method, items(pair))
        owner, role = owners[caller]
        target, target_role = owners[callee]
        references.append({"from": owner, "to": target, "caller": caller, "callee": callee})
        if role in ("HandlerMethod", "ScopeOpen"):
            calls[owner].append(target)
        if role in ("HandlerMethod", "ScopeOpen", "ContextRequest") and target_role == "ContextRequest":
            request_calls[owner].add(target)
    # A session request is deliberately absent from graphReferences. Its resolved demand
    # identifies the delivery; ordinary producer calls are edges in the compiled graph.
    requests = {n: [p for p in resolved_requests[n] if p in request_calls[n] or specs[p]["stream"]]
                for n in effects}
    selections = defaultdict(list)
    tree = workflow(value["protoWorkflow"], selections)
    chains, sources = consumer_chains(effects, inputs, requests, selections, calls)
    roles = {"HandlerMethod": "handler", "ScopeOpen": "open", "ScopeClose": "close", "PureHandler": "handler",
             "StreamHandler": "handler", "ContextRequest": "parser", "ContextStream": "parser",
             "ErrorDetect": "detect", "ErrorResolve": "resolve", "MiddlewareHook": "handler"}
    bindings = []
    for definition in items(graph["graphMethods"]):
        _, (identifier, _, _, body) = application(definition)
        m = method(identifier)
        n, role = owners[m]
        bindings.append({"effect": n, "method": m, "handler": specs[n].get(roles.get(role)), "source": None,
                         "shape": body.text.decode().strip(), "reachable": reaches.get(m)})
    return {"schemaVersion": 1, "namespace": namespace, "source": "prototype", "chains": chains,
            "assetSources": sources, "registrations": [{"effect": e} for e in effects.values()],
            "bindings": bindings, "references": references, "workflow": tree}


def read_prototype(folder: Path, namespace: str) -> dict | None:
    path = folder / "Generated" / "Prototype.hs"
    if path.resolve() != path:
        raise ValueError("Prototype.hs 路径超出绑定目录。")
    try:
        with path.open("rb") as handle:
            raw = handle.read()
            modified = os.fstat(handle.fileno()).st_mtime
    except FileNotFoundError:
        return None
    root = Parser(HASKELL).parse(raw).root_node
    if root.has_error:
        raise ValueError("Generated/Prototype.hs 存在 Haskell 语法错误。")
    try:
        data = project(root, namespace)
    except (KeyError, IndexError, ValueError, StopIteration, RecursionError) as exc:
        raise ValueError(f"无法读取 Generated/Prototype.hs 的图结构：{exc}") from exc
    data["analyzedAt"] = datetime.fromtimestamp(modified, timezone.utc).isoformat()
    handlers = path.with_name("Handlers.hs")
    if handlers.resolve() == handlers and handlers.is_file():
        definitions = Parser(HASKELL).parse(handlers.read_bytes()).root_node.child_by_field_name("declarations")
        bodies = {}
        for decl in definitions.named_children if definitions else ():
            methods = decl.child_by_field_name("declarations") if decl.type == "instance" else None
            for m in methods.named_children if methods else ():
                if m.type in ("bind", "function") and (ident := m.child_by_field_name("name")):
                    bodies[name(ident)] = m.text.decode()
        for b in data["bindings"]:
            b["source"] = bodies.get(b["handler"])
    return data
