"""Beforegen. Return a candidate expression only after the SDK can read it back."""

from ..analysis.read import read_effect
from ..framework import Framework
from ..project.model import TYPING_FIELDS, validate_effect


def literal(text: str) -> str:
    # Haskell numeric escapes consume digits until a non-digit or an empty escape.
    pieces = []
    for c in text:
        if c in ('"', "\\"):
            pieces.append("\\" + c)
        elif not c.isprintable():
            pieces.append(f"\\x{ord(c):x}\\&")
        else:
            pieces.append(c)
    return '"' + "".join(pieces) + '"'


def array(entries) -> str:
    return "[" + ", ".join(entries) + "]"


def typing_field(key, value) -> str:
    if key == "asset":
        return f"Asset @{value['type']} {literal(value['contract'])}"
    if key in ("require", "stream"):
        return "True" if value else "False"
    if key == "handles":
        return array(f"{t['kind']} {literal(t['target'])}" for t in value)
    if key == "intercept":
        return array(value)
    return "Nothing" if value is None else "Just " + literal(value)


def generate_effect(document: dict, framework: Framework) -> str:
    effect = validate_effect(document)
    typing = effect["typing"]
    kind = typing["kind"]
    body = ", ".join(f"{k} = {typing_field(k, typing[k])}" for k in TYPING_FIELDS[kind])
    demands = array(f"{d['access']} {literal(d['contract'])}" for d in effect["demands"])
    source = (
        "EffectSpec\n"
        f"  {{ name = {literal(effect['name'])}\n"
        f"  , typing = {kind} {{ {body} }}\n"
        f"  , input = {literal(effect['input'])}\n"
        f"  , demands = {demands}\n"
        "  }"
    )
    if read_effect(source, framework) != document:
        raise ValueError("Generated registration differs from the requested model")
    return source
