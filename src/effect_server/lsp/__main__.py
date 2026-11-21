"""Local stdin/stdout entry point for the effect template tools."""

import argparse
import json
from pathlib import Path
import sys

from .analysis.facade import facade
from .analysis.read import read_effect, read_namespace
from .analysis.sources import namespace_folder, source_document
from .codegen.effect import generate_effect
from .framework import Framework
from .project.entries import entry_for
from .runtime.snapshot import publish


def unique_fields(pairs):
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError(f"Duplicate JSON field: {key}")
        fields[key] = value
    return fields


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, help="built effect-codegen executable (SDK commands)")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="trusted framework checkout")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("read", help="read one EffectSpec expression from UTF-8 stdin")
    commands.add_parser("generate", help="render and verify one JSON document from UTF-8 stdin")
    inspect = commands.add_parser("inspect", help="read registrations in a trusted namespace")
    inspect.add_argument("namespace")
    projection = inspect.add_mutually_exclusive_group()
    projection.add_argument("--name", help="select a registration by its exact effect name")
    projection.add_argument("--cards", action="store_true", help="read the Facade workbench card model")
    commands.add_parser("snapshot", help="publish the console graph and workflow").add_argument("namespace")
    review = commands.add_parser("gened", help="inspect an on-disk Generated Haskell module")
    review.add_argument("namespace")
    review.add_argument("--file", help="module path relative to Generated")
    args = parser.parse_args()
    if args.command != "gened" and not args.sdk:
        parser.error("--sdk is required for SDK commands")
    try:
        root = args.root.resolve()
        if args.command == "gened":
            folder = namespace_folder(root, args.namespace)
            result = source_document(folder, True, args.file)
            sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
            return
        framework = (Framework(args.sdk, root, entry_for(root, args.namespace).source_dir)
                     if args.command in {"snapshot", "inspect"} else Framework(args.sdk, root))
        if args.command == "snapshot":
            print(publish(args.namespace, framework))
            return
        if args.command == "generate":
            model = json.loads(sys.stdin.buffer.read(), object_pairs_hook=unique_fields)
            source = generate_effect(model, framework)
            sys.stdout.buffer.write((source + "\n").encode("utf-8"))
            return
        if args.command == "read":
            result = read_effect(sys.stdin.buffer.read().decode("utf-8-sig"), framework)
        elif args.cards:
            result = facade(framework, args.namespace)
            if "error" in result:
                raise ValueError(result["error"])
        else:
            result = read_namespace(args.namespace, framework)
            if args.name is not None:
                matches = [r for r in result["registrations"] if r["effect"]["name"] == args.name]
                if len(matches) != 1:
                    raise ValueError(f"Expected one registration named {args.name!r}, found {len(matches)}")
                if not result["editable"]:
                    print("Read-only declaration: projecting its value as a new template.", file=sys.stderr)
                result = {"schemaVersion": result["schemaVersion"], "effect": matches[0]["effect"]}
        encoded = json.dumps(result, ensure_ascii=False, indent=2)
        sys.stdout.buffer.write((encoded + "\n").encode("utf-8"))
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
