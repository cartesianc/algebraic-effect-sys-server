"""Registration relationships used as evidence when writing a Handler."""

from collections import defaultdict

from ..runtime.workflow import report_state


ROLES = ("handler", "parser", "open", "close", "detect", "resolve")


def methods(effect):
    return {value for role in ROLES if (value := effect["typing"].get(role))}


def mounted_effects(report):
    targets = defaultdict(list)
    for mount in report.get("hanging", []):
        for target in mount["targets"]:
            for path in target["paths"]:
                targets[tuple(path)].extend((hook, target["name"]) for hook in mount["hooks"])

    def visit(node, inherited):
        scope = inherited + targets[tuple(node["path"])]
        if node.get("effect"):
            for hook, branch in scope:
                yield hook, node["effect"], "mountedScope", branch
        for child in node.get("children", []):
            yield from visit(child, scope)

    yield from visit(report["workflow"], [])


def neighborhood(effects, selected, root, folder, namespace):
    names = {effect["name"] for effect in effects}
    owners, producers = defaultdict(list), defaultdict(list)
    edges = set()
    for effect in effects:
        name = effect["name"]
        for method in methods(effect):
            owners[method].append(name)
        if asset := effect["typing"].get("asset"):
            producers[asset["contract"]].append(name)
        if effect["input"] in names:
            edges.add((effect["input"], name, "input", ""))
    for method, bindings in owners.items():
        for index, first in enumerate(bindings):
            for other in bindings[index + 1:]:
                edges.add((first, other, "sharedHandler", method))
    for effect in effects:
        name = effect["name"]
        for demand in effect["demands"]:
            for provider in producers[demand["contract"]]:
                edges.add((provider, name, demand["access"], demand["contract"]))
        for handle in effect["typing"].get("handles", []):
            kind, target = handle["kind"], handle["target"]
            for source in owners[target] if kind == "RaisedBy" else [target]:
                if source in names:
                    edges.add((source, name, kind, target))
    workflow = report_state(root, folder, namespace)
    if workflow["state"] == "fresh":
        edges.update(edge for edge in mounted_effects(workflow["report"]) if edge[0] in names and edge[1] in names)
    neighbors = defaultdict(set)
    for first, second, _, _ in edges:
        neighbors[first].add(second)
        neighbors[second].add(first)
    seen, todo = {selected}, [selected]
    while todo:
        new = neighbors[todo.pop()] - seen
        seen.update(new)
        todo.extend(new)
    return ([e for e in effects if e["name"] in seen],
            [{"from": a, "to": b, "relation": kind, "via": via} for a, b, kind, via in sorted(edges) if a in seen],
            workflow["state"])
