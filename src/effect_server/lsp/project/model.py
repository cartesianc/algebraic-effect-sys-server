"""Editable EffectSpec v1, shared by the source reader and template writer."""

VERSION = 1
TYPING_FIELDS = {
    "Pure": ("handler",),
    "Excu": ("handler",),
    "Stream": ("handler",),
    "Scoped": ("open", "close"),
    "Error": ("detect", "resolve", "handles"),
    "Context": ("asset", "parser", "require", "stream"),
    "Middleware": ("handler", "intercept"),
}
ACCESS = ("requests", "requires", "streams")
HANDLES = ("RaisedBy", "ScopeFaultIn", "ContextFaultIn")
INTERCEPT = ("ExcuKind", "ScopedKind", "PureKind", "StreamKind")


def fields(value, names, label):
    if not isinstance(value, dict) or set(value) != set(names):
        raise ValueError(f"{label} expects exactly {', '.join(names)}")


def string(value, label):
    if not isinstance(value, str) or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ValueError(f"{label} expects a Unicode string")


def choice(value, options, label):
    if not isinstance(value, str) or value not in options:
        raise ValueError(f"{label} expects one of {', '.join(options)}")


def items(value, label):
    if not isinstance(value, list):
        raise ValueError(f"{label} expects an array")
    return value


def constructor_name(name):
    return bool(name) and name[0].isupper() and all(
        c.isalnum() or c in "_'" for c in name[1:]
    )


def validate_effect(document):
    fields(document, ("schemaVersion", "effect"), "document")
    if type(document["schemaVersion"]) is not int or document["schemaVersion"] != VERSION:
        raise ValueError("Unsupported effect schemaVersion")
    effect = document["effect"]
    fields(effect, ("name", "typing", "input", "demands"), "EffectSpec")
    string(effect["name"], "name")
    string(effect["input"], "input")
    for demand in items(effect["demands"], "demands"):
        fields(demand, ("access", "contract"), "demand")
        choice(demand["access"], ACCESS, "access")
        string(demand["contract"], "contract")
    typing = effect["typing"]
    kind = typing.get("kind") if isinstance(typing, dict) else None
    choice(kind, TYPING_FIELDS, "typing.kind")
    fields(typing, ("kind", *TYPING_FIELDS[kind]), kind)
    for key in TYPING_FIELDS[kind]:
        value = typing[key]
        if key == "asset":
            fields(value, ("type", "contract"), "asset")
            string(value["contract"], "asset.contract")
            spelling = value["type"]
            string(spelling, "asset.type")
            if spelling != "[String]" and not all(constructor_name(p) for p in spelling.split(".")):
                raise ValueError("asset.type expects a type constructor or [String]")
        elif key in ("require", "stream"):
            if type(value) is not bool:
                raise ValueError(f"{key} expects a boolean")
        elif key == "handles":
            for target in items(value, key):
                fields(target, ("kind", "target"), "handles entry")
                choice(target["kind"], HANDLES, key)
                string(target["target"], "target")
        elif key == "intercept":
            for target in items(value, key):
                choice(target, INTERCEPT, key)
        elif value is not None:
            string(value, key)
    return effect
