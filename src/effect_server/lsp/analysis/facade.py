"""Project handwritten registrations and their method bindings onto workbench cards."""

from tree_sitter import Parser

from ..framework import Framework
from ..runtime.snapshot import RegistrationConflict
from .read import read_namespace
from .sources import HASKELL, modules, namespace_folder, revision


def list_entries(raw: bytes, count: int):
    """An explicit outer list preserves the order of its evaluated EffectSpecs, including helpers."""
    root = Parser(HASKELL).parse(raw).root_node
    block = root.child_by_field_name("declarations")
    if root.has_error or not block:
        return []
    bindings = [d for d in block.named_children if d.type == "bind"
                and (name := d.child_by_field_name("name")) and name.text == b"effects"]
    if len(bindings) == 1:
        matches = bindings[0].children_by_field_name("match")
        if len(matches) == 1 and not matches[0].child_by_field_name("guards"):
            expr = matches[0].child_by_field_name("expression")
            if expr and expr.type == "list":
                entries = expr.children_by_field_name("element")
                if len(entries) == count:
                    return entries
    return []


def method_nodes(raw: bytes, names: set[str]):
    block = Parser(HASKELL).parse(raw).root_node.child_by_field_name("declarations")
    for instance in block.named_children if block else ():
        if instance.type != "instance":
            continue
        cls = instance.child_by_field_name("name")
        methods = instance.child_by_field_name("declarations")
        if not cls or not methods:
            continue
        cls_name = cls.text.decode().rsplit(".", 1)[-1]
        for method in methods.named_children:
            name = method.child_by_field_name("name")
            if method.type not in ("bind", "function") or not name:
                continue
            handler = name.text.decode()
            if handler in names and cls_name == handler[:1].upper() + handler[1:] + "Handler":
                yield handler, method


def method_definitions(raw: bytes, names: set[str]) -> dict:
    result = {name: [] for name in names}
    digest = revision(raw)
    for name, method in method_nodes(raw, names):
        result[name].append({
            "file": "Handlers.hs", "revision": digest, "line": method.start_point.row + 1,
            "endLine": method.end_point.row + 1, "source": method.text.decode(),
        })
    return result


def card_graph(rows: list, effects: bytes, handlers: bytes) -> dict:
    cards = [{"id": "Root", "label": "Root", "kind": "Node", "category": "root"}]
    edges = []
    assets = {}
    owners = {}
    inputs = {row["effect"]["name"]: row["effect"]["input"] for row in rows}
    entries = list_entries(effects, len(rows)) if any("range" not in r for r in rows) else []
    digest = revision(effects)
    for i, row in enumerate(rows):
        effect = row["effect"]
        name, typing = effect["name"], effect["typing"]
        bindings = []
        for role in ("handler", "parser", "open", "close", "detect", "resolve"):
            if handler := typing.get(role):
                bindings.append({"role": role, "name": handler})
                uses = owners.setdefault(handler, [])
                if name not in uses:
                    uses.append(name)
        card = {"id": name, "label": name, "kind": typing["kind"], "category": "effect", "bindings": bindings}
        if "range" in row or entries:
            first, last = ((row["range"][0], row["range"][2]) if "range" in row else
                           (entries[i].start_point.row + 1, entries[i].end_point.row + 1))
            card["location"] = {"file": "Effects.hs", "revision": digest, "line": first, "endLine": last}
        cards.append(card)
        if effect["input"] != "None":
            edges.append({"from": effect["input"], "to": name, "access": "input"})
        if asset := typing.get("asset"):
            assets[asset["contract"]] = (asset["type"], name)
        for demand in effect["demands"]:
            contract = demand["contract"]
            assets.setdefault(contract, (None, None))
            edges.append({"from": "asset:" + contract, "to": name, "access": demand["access"]})
    production_steps = set()
    for contract, (representation, producer) in assets.items():
        chain = []
        seen = set()
        while producer in inputs and producer not in seen:
            chain.append(producer)
            seen.add(producer)
            producer = inputs[producer]
        cards.append({"id": "asset:" + contract, "label": contract, "kind": "Asset", "category": "asset",
                      "representation": representation, "producerChain": chain[::-1]})
        production_steps.update(chain)
    # A Pure or Context on an ordinary effect's Input path remains part of the overview.
    production_only = {c["id"] for c in cards if c["kind"] in {"Pure", "Context"} and c["id"] in production_steps}
    for effect in inputs.keys() - production_only:
        parent = inputs[effect]
        while parent in production_only:
            production_only.remove(parent)
            parent = inputs[parent]
    for card in cards:
        if card["id"] in production_only:
            card["productionOnly"] = True
    definitions = method_definitions(handlers, set(owners))
    return {"cards": cards, "edges": edges, "handlers": {
        h: {"effects": uses, "definitions": definitions[h]} for h, uses in owners.items()
    }}


def facade(framework: Framework, namespace: str) -> dict:
    files = modules(namespace_folder(framework.root, namespace, framework.source_dir), False)
    base = {"namespace": namespace, "files": list(files), "editable": False, "registrations": []}
    if "Effects.hs" not in files:
        return {**base, "error": "缺少 Effects.hs"}
    effects = files["Effects.hs"]
    raw = effects.read_bytes()
    base["revision"] = "facade:" + revision(raw)
    try:
        data = read_namespace(namespace, framework)
    except ValueError as exc:
        data = {"error": str(exc)}
    if effects.read_bytes() != raw:
        raise RegistrationConflict("读取期间 Effects.hs 已更改，请刷新。")
    if "error" in data:
        return {**base, **data}
    handlers = files["Handlers.hs"].read_bytes() if "Handlers.hs" in files else b""
    return {**base, **data, "graph": card_graph(data["registrations"], raw, handlers)}
