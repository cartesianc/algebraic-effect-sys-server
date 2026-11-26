"""Read the SDK's emitted Haskell value without loading or executing a module."""

import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from tree_sitter import Parser

from .generated_syntax import application, field, items, name, record, string
from .program import compiled_relations, handler_reads
from .sources import HASKELL


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


def consumer_chains(effects, inputs, providers, selected, entry_calls, continuous, callback_only):
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
            # The legacy "session" flag also keeps an externally delivered asset
            # separate from the reader's Input predecessors in the graph layout.
            delivered = ctx in continuous or (feeds or n, asset) in callback_only
            branch = dict(source, context=ctx, asset=asset, feeds=feeds, via=via, session=delivered)
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


def project(root, namespace, handlers, program):
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
    continuous, sampled, histories, resources = set(), set(), set(), set()
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
        if specs[n].get("stream") or ("effectContinuous" in fields and name(fields["effectContinuous"]) == "True"):
            continuous.add(n)
        if "effectSampled" in fields and name(fields["effectSampled"]) == "True":
            sampled.add(specs[n]["asset"]["contract"])
        if "effectHistory" in fields and name(fields["effectHistory"]) == "True":
            histories.add(specs[n]["asset"]["contract"])
        if "effectResource" in fields:
            tag, resource = application(fields["effectResource"])
            if tag == "Just":
                resources.add(specs[string(resource[0])]["asset"]["contract"])
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
    # A continuous request can start a source without directly calling its parser.
    requests = {n: [p for p in resolved_requests[n] if p in request_calls[n] or p in continuous]
                for n in effects}
    selections = defaultdict(list)
    tree = workflow(value["protoWorkflow"], selections)
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
    data = {"schemaVersion": 1, "namespace": namespace, "source": "prototype",
            "registrations": [{"effect": e} for e in effects.values()],
            "bindings": bindings, "references": references, "workflow": tree}
    reads = {}
    if handlers:
        definitions = handlers.child_by_field_name("declarations")
        bodies = {}
        bound = {b["handler"] for b in bindings if b["handler"]}
        for decl in definitions.named_children if definitions else ():
            methods = decl.child_by_field_name("declarations") if decl.type == "instance" else None
            for m in methods.named_children if methods else ():
                if m.type in ("bind", "function") and (ident := m.child_by_field_name("name")):
                    handler = name(ident)
                    if handler in bound:
                        bodies[handler] = m.text.decode()
                        reads.setdefault(handler, set()).update(handler_reads(m))
        for b in bindings:
            b["source"] = bodies.get(b["handler"])
    callback_only = set()
    if program:
        try:
            relations = compiled_relations(program, data, reads, sampled, histories, resources)
        except (KeyError, IndexError, ValueError, StopIteration, RecursionError) as exc:
            raise ValueError(f"Generated/Program.hs 的执行闭包无效：{exc}") from exc
        if relations is not None:
            data["assetRelations"], callback_only = relations
    data["chains"], data["assetSources"] = consumer_chains(effects, inputs, requests, selections, calls, continuous, callback_only)
    return data


def read_prototype(folder: Path, namespace: str) -> dict | None:
    modified = 0.0

    def module(filename):
        nonlocal modified
        path = folder / "Generated" / filename
        if path.resolve() != path:
            if filename == "Handlers.hs":
                return None
            raise ValueError(f"{filename} 路径超出绑定目录。")
        try:
            with path.open("rb") as handle:
                raw = handle.read()
                modified = max(modified, os.fstat(handle.fileno()).st_mtime)
        except FileNotFoundError:
            return None
        return Parser(HASKELL).parse(raw).root_node

    root = module("Prototype.hs")
    if root is None:
        return None
    if root.has_error:
        raise ValueError("Generated/Prototype.hs 存在 Haskell 语法错误。")
    try:
        data = project(root, namespace, module("Handlers.hs"), module("Program.hs"))
    except (KeyError, IndexError, ValueError, StopIteration, RecursionError) as exc:
        raise ValueError(f"无法读取 {namespace} 的生成图：{exc}") from exc
    data["analyzedAt"] = datetime.fromtimestamp(modified, timezone.utc).isoformat()
    return data
