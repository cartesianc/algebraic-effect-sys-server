"""Project the generated execution closures onto asset-to-reader relations."""

import re

from .generated_syntax import application, descendants, field, items, name, record, string


def type_key(text):
    # The SDK's normalizeType ignores qualifiers/parentheses and expands String and FilePath.
    text = re.sub(r"\b(?:[A-Z][\w']*\.)+|[()]", "", text)
    return re.sub(r"\s+", "", re.sub(r"\b(?:String|FilePath)\b", "[Char]", text))


def handler_reads(method):
    reads = set()
    for node in descendants(method):
        if node.type == "apply":
            arg = node.child_by_field_name("argument")
            if arg is not None and arg.type == "type_application":
                function = name(field(node, "function"))
                if function in ("stream", "require"):
                    reads.add((function, type_key(field(arg, "type").text.decode())))
    return reads


def coordinates(node):
    return tuple(int(i.text) for i in items(node))


def compiled_relations(root, data, reads, sampled, histories, resources):
    if root.has_error:
        raise ValueError("Haskell 语法错误")
    header = next(n for n in root.named_children if n.type == "header")
    if field(header, "module").text.decode() != data["namespace"] + ".Generated.Program":
        raise ValueError("模块名与命名空间不符")
    decls = field(root, "declarations")
    bind = next((n for n in decls.named_children if n.type == "bind"
                 and name(field(n, "name")) == "compiledProgram"), None)
    if bind is None:  # Earlier SDKs emitted compiledEntries and used runtime sessions.
        return None
    expr = field(field(bind, "match"), "expression")
    compiled = next(n for n in descendants(expr) if n.type == "record" and name(field(n, "expression")) == "Compiled")
    fields = record(compiled)
    effects = {row["effect"]["name"]: row["effect"] for row in data["registrations"]}
    slot_numbers = {label: index for index, label in enumerate(effects)}
    workflow = {}
    nodes = [data["workflow"]]
    while nodes:
        node = nodes.pop()
        workflow[tuple(node["path"])] = node
        nodes.extend(node["children"])
    types = {t["asset"]["contract"]: type_key(t["asset"]["type"]) for e in effects.values()
             if (t := e["typing"]).get("asset")}
    methods = {}
    for b in data["bindings"]:
        if b["handler"] in reads:
            methods.setdefault(b["effect"], set()).update(reads[b["handler"]])

    def asset_of(slot):
        tag, args = application(slot)
        if tag != "Slot" or len(args) != 2:
            raise ValueError("预期生成程序的资产 Slot")
        label = string(args[1])
        if slot_numbers.get(label) != int(args[0].text):
            raise ValueError(f"{label} 的 Slot 与 Prototype 不符")
        return effects[label]["typing"]["asset"]["contract"]

    def target_effect(target):
        tag, (label, _) = application(target)
        if tag != "target":
            raise ValueError("预期生成程序的 target")
        effect = string(label).partition("@")[0]
        if effect not in effects:
            raise ValueError(f"Prototype 中缺少 {effect}")
        return effect

    # A static recipe exists for each asset the SDK can collect. A particular reader
    # starts it only where its generated execution actually requests the asset.
    comparators = set()

    def stream_source(node):
        tag, args = application(node)
        # Newer SDKs append the source's tracking mark (gen's reclamation-free judgment).
        if len(args) == 5:
            *args, tracking = args
            if application(tracking) not in (("Tracked", []), ("Untracked", [])):
                raise ValueError("StreamSource 的回收跟踪标记无效")
        if len(args) != 4:
            raise ValueError("StreamSource 的参数个数与 SDK 不符")
        slot, sender, parser, comparator = args
        asset = asset_of(slot)
        option, _ = application(comparator)
        if tag != "StreamSource" or option not in ("Just", "Nothing") or (option == "Just") != (asset in sampled):
            raise ValueError(f"{asset} 的来源比较器与 Prototype 的 effectSampled 不符")
        if option == "Just":
            comparators.add(asset)
        return [items(sender), items(parser)]

    static, sources = {}, {}
    for row in items(fields["compiledStatic"]):
        slot, scope, source = items(row)
        path = coordinates(scope)
        static[asset_of(slot)] = path
        sources[path] = stream_source(source)
    pending_static, active_static = set(), set()
    subscriptions, producers = set(), set()
    relations = {}

    def add(asset, reader, access, mode, path, scope):
        relations[asset, reader, access, mode, path, scope] = dict(
            asset=asset, reader=reader, access=access, mode=mode, path=list(path),
            scope=list(scope) if scope is not None else None)

    def mounted(reader, path, driver=None, scope=None, parallel_sources=frozenset()):
        # A terminal can inherit its driving streams demand from an Input predecessor.
        seen = set()
        while reader in effects and reader not in seen:
            seen.add(reader)
            effect = effects[reader]
            read = methods.get(reader, set())
            requests = {d["contract"] for d in effect["demands"] if d["access"] == "requests"}
            for d in effect["demands"]:
                asset, access = d["contract"], d["access"]
                if asset == driver and access in ("streams", "requests"):
                    subscriptions.add((reader, asset))
                    add(asset, reader, access, "onChange" if asset in sampled else "onPublish", path, scope)
                elif access == "requests":
                    producers.add((reader, asset))
                    if asset in static:
                        add(asset, reader, access, "collect", path, static[asset])
                        pending_static.add(static[asset])
                if access == "requires":
                    mode = "latest"
                    if ("require", types[asset]) in read:
                        if asset == driver:
                            mode = "delivered"
                        elif asset in parallel_sources and asset not in resources and asset not in requests:
                            mode = "await"
                    add(asset, reader, access, mode, path, scope)
                elif access == "streams":
                    read_scope = scope if driver else static.get(asset)
                    if asset in histories and ("stream", types[asset]) in read:
                        add(asset, reader, access, "history", path, read_scope)
                    if ("require", types[asset]) in read:
                        add(asset, reader, access, "delivered" if driver == asset else "latest", path, read_scope)
            # A mounted parser consumes the output it has just been given. It does
            # not invoke the source's Input chain again on each delivery.
            if effect["typing"]["kind"] == "Context":
                break
            reader = effect["input"]

    visited = set()
    kinds = {"Step": "Node", "Serial": "Chain", "Concurrent": "Parallel", "Choose": "Alternative", "Drive": "Lifelong"}

    def closure(node, driver=None, scope=None, parallel_sources=frozenset()):
        tag, args = application(node)
        if tag not in kinds or not args:
            raise ValueError(f"未知的执行闭包 {tag}")
        path = coordinates(args[0])
        if path not in workflow or kinds.get(tag) != workflow[path]["kind"]:
            raise ValueError(f"执行位置 {path} 与 Prototype 的 Workflow 不符")
        visited.add(path)
        if tag == "Step":
            effect = target_effect(args[1])
            if effect != workflow[path]["effect"]:
                raise ValueError(f"执行位置 {path} 的 {effect} 与 Prototype 不符")
            if driver and not any(d["contract"] == driver and d["access"] in ("requests", "streams")
                                  for d in effects[effect]["demands"]):
                # The Step itself runs on publication even if only its Input
                # predecessor declares the streams read that drives the unit.
                add(driver, effect, "callback", "onChange" if driver in sampled else "onPublish", path, scope)
            mounted(effect, path, driver, scope, parallel_sources)
        elif tag == "Serial":
            for child in items(args[1]):
                closure(child, driver, scope, parallel_sources)
        elif tag == "Concurrent":
            branches = []
            rows = items(args[1])
            paired = [row.type == "tuple" for row in rows]
            if any(paired) and not all(paired):
                raise ValueError("Concurrent 的分支不能混用两种生成格式")
            for row in rows:
                if row.type == "tuple":
                    slots, child = items(row)
                    if slots.type != "list":
                        raise ValueError("Concurrent 分支缺少可能发布的资产列表")
                    branches.append((child, {asset_of(slot) for slot in items(slots)}))
                else:  # Older closures contain only the child, without publisher evidence.
                    branches.append((row, set()))
            for index, (child, _) in enumerate(branches):
                others = (slots for sibling, (_, slots) in enumerate(branches) if sibling != index)
                peers = parallel_sources.union(*others)
                closure(child, driver, scope, peers)
        elif tag == "Choose":
            mounted(target_effect(args[2]), coordinates(args[1]), driver, scope, parallel_sources)
            for row in items(args[3]):
                closure(items(row)[2], driver, scope, parallel_sources)
        elif tag == "Drive":
            option, source = application(args[1])
            if option == "Just":
                for at, body in stream_source(source[0]):
                    mounted(target_effect(body), coordinates(at), scope=path, parallel_sources=parallel_sources)

    closure(fields["compiledWorkflow"])
    derived = []
    for node in items(fields["compiledReactions"]):
        tag, (slot, region, reaction) = application(node)
        if tag != "Reaction":
            raise ValueError(f"未知的回调记录 {tag}")
        asset = asset_of(slot)
        option, region_args = application(region)
        scope = coordinates(region_args[0]) if option == "Just" else None
        action, args = application(reaction)
        if action == "RunUnit":
            if len(args) == 3:
                mount, tracking, target = args
                if application(tracking) not in (("Tracked", []), ("Untracked", [])):
                    raise ValueError("RunUnit 的回收跟踪标记无效")
            else:
                mount, target = args
            path = coordinates(mount)
            if path != coordinates(application(target)[1][0]) or (scope is not None and scope != path[:-1]):
                raise ValueError("回调的挂载坐标与目标不符")
            closure(target, asset, scope)
        elif action == "RunDerive":
            mount, target = args
            derived.append((asset, target_effect(target), coordinates(mount), scope))
        else:
            raise ValueError(f"未知的回调 {action}")
    if visited != workflow.keys():
        raise ValueError("Program 与 Prototype 包含的 Workflow 节点不一致")
    while pending_static:
        scope = pending_static.pop()
        if scope not in active_static:
            active_static.add(scope)
            for at, body in sources[scope]:
                mounted(target_effect(body), coordinates(at), scope=scope)
    for asset, target, path, scope in derived:
        if scope not in sources or scope in active_static:
            add(asset, target, "derive", "onChange" if asset in sampled else "onPublish", path, scope)
    if any(r["mode"] == "onChange" and r["asset"] not in comparators for r in relations.values()):
        raise ValueError("变化订阅缺少 Program 中的来源比较器")
    return list(relations.values()), subscriptions - producers
