"""Syntax shared by readers of the SDK's generated Haskell values."""

import re


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


def descendants(node):
    pending = [node]
    while pending:
        current = pending.pop()
        yield current
        pending.extend(reversed(current.named_children))
