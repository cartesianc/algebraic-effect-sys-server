"""Aftergen. A single expression needs no business module or evaluation."""

from ..framework import Framework
from ..project.model import VERSION, validate_effect


def read_effect(source: str, framework: Framework) -> dict:
    document = framework.read_json("read-effect", source=source)
    validate_effect(document)
    return document


def read_namespace(namespace: str, framework: Framework, *, timeout: float | None = None) -> dict:
    """Read the effects of a trusted namespace; helper-built registrations are read-only."""
    result = framework.read_json("read-effects", namespace, timeout=timeout)
    if result.get("schemaVersion") != VERSION:
        raise ValueError("Unsupported namespace schemaVersion")
    if result["editable"]:
        for row in result["registrations"]:
            validate_effect({"schemaVersion": VERSION, "effect": row["effect"]})
    return result
