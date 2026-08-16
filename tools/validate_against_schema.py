#!/usr/bin/env python3
"""Check a generated ITR JSON against the department's own published schema.

This closes the largest open caveat in the project. Everything else here is
tested — the arithmetic against the statute, the parsers against fixtures — but
the *element names* in the generated JSON were written from the published
schema by hand and never checked against it. A rejected import is a naming
problem rather than a tax one, but it is still a rejected import.

The department publishes the schema alongside each offline utility, at
e-Filing portal → Downloads → Income Tax Returns → the assessment year. The
files are named like ``ITR-3_2026_Main_V1.1.json``. Download the one for your
form and year, then:

    python tools/validate_against_schema.py ITR-3_2026_Main_V1.1.json return.json

It reports three things, and the first is the one that matters:

* **elements we emit that the schema does not define** — these are the
  invented names, and each one is a probable rejection
* **required elements we do not emit**
* **type and format mismatches**

The schema files have varied in shape between years — some are JSON Schema
proper, some are a plainer structural description — so this inspects the file
before deciding how to read it rather than assuming.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set, Tuple


def load(path: Path) -> Any:
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def looks_like_json_schema(document: Any) -> bool:
    if not isinstance(document, dict):
        return False
    return bool(
        document.get("$schema")
        or ("properties" in document and "type" in document)
        or "definitions" in document
    )


# --------------------------------------------------------------------------
# Structural comparison — works whatever the schema's flavour
# --------------------------------------------------------------------------


def schema_paths(node: Any, prefix: str = "") -> Set[str]:
    """Every element path the schema defines.

    Walks ``properties``, ``definitions``/``$defs`` and array ``items`` alike,
    so a plain structural description and a JSON Schema both resolve to the
    same flat set of dotted paths.
    """
    found: Set[str] = set()
    if not isinstance(node, dict):
        return found

    for container in ("definitions", "$defs"):
        for name, child in (node.get(container) or {}).items():
            found |= schema_paths(child, "")

    properties = node.get("properties")
    if isinstance(properties, dict):
        for name, child in properties.items():
            path = f"{prefix}.{name}" if prefix else name
            found.add(path)
            found |= schema_paths(child, path)

    items = node.get("items")
    if isinstance(items, dict):
        found |= schema_paths(items, prefix)

    for keyword in ("allOf", "anyOf", "oneOf"):
        for child in node.get(keyword) or []:
            found |= schema_paths(child, prefix)

    # A plainer description: a bare nested object with no schema keywords.
    if not properties and not items and prefix:
        for name, child in node.items():
            if name.startswith("$") or name in _KEYWORDS:
                continue
            if isinstance(child, (dict, list)):
                path = f"{prefix}.{name}"
                found.add(path)
                if isinstance(child, dict):
                    found |= schema_paths(child, path)
    return found


_KEYWORDS = {
    "type", "required", "properties", "items", "definitions", "$defs",
    "allOf", "anyOf", "oneOf", "enum", "minimum", "maximum", "minLength",
    "maxLength", "pattern", "description", "title", "default", "format",
    "additionalProperties", "minItems", "maxItems",
}


def document_paths(node: Any, prefix: str = "") -> Set[str]:
    """Every element path the generated return actually uses."""
    found: Set[str] = set()
    if isinstance(node, dict):
        for name, child in node.items():
            path = f"{prefix}.{name}" if prefix else name
            found.add(path)
            found |= document_paths(child, path)
    elif isinstance(node, list):
        for child in node:
            found |= document_paths(child, prefix)
    return found


def leaf(path: str) -> str:
    return path.rsplit(".", 1)[-1]


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(1)

    schema_path, return_path = Path(sys.argv[1]), Path(sys.argv[2])
    schema = load(schema_path)
    document = load(return_path)

    print(f"schema:  {schema_path.name}")
    print(f"return:  {return_path.name}")
    print(f"shape:   {'JSON Schema' if looks_like_json_schema(schema) else 'structural description'}\n")

    # ---- Formal validation, where the file supports it --------------------
    if looks_like_json_schema(schema):
        try:
            import jsonschema

            validator = jsonschema.Draft7Validator(schema)
            errors = sorted(validator.iter_errors(document), key=lambda e: e.path)
            if errors:
                print(f"--- {len(errors)} schema violation(s) ---")
                for error in errors[:40]:
                    location = ".".join(str(p) for p in error.absolute_path) or "(root)"
                    print(f"  {location}: {error.message[:160]}")
                if len(errors) > 40:
                    print(f"  ... {len(errors) - 40} more")
            else:
                print("--- no schema violations ---")
            print()
        except ImportError:
            print("(install jsonschema for formal validation: pip install jsonschema)\n")

    # ---- Name-level comparison, which is where the guesses show up --------
    defined = schema_paths(schema)
    used = document_paths(document)
    defined_leaves = {leaf(p) for p in defined}
    used_leaves = {leaf(p) for p in used}

    invented = sorted(used_leaves - defined_leaves)
    print(f"--- {len(invented)} element name(s) we emit that the schema does not define ---")
    for name in invented:
        print(f"  {name}")
    if not invented:
        print("  (none — every name we emit exists in the schema)")

    required = _required_names(schema)
    missing = sorted(required - used_leaves)
    print(f"\n--- {len(missing)} required element(s) we do not emit ---")
    for name in missing[:60]:
        print(f"  {name}")
    if len(missing) > 60:
        print(f"  ... {len(missing) - 60} more")
    if not missing:
        print("  (none)")

    raise SystemExit(1 if invented else 0)


def _required_names(node: Any) -> Set[str]:
    found: Set[str] = set()
    if isinstance(node, dict):
        for name in node.get("required") or []:
            if isinstance(name, str):
                found.add(name)
        for child in node.values():
            found |= _required_names(child)
    elif isinstance(node, list):
        for child in node:
            found |= _required_names(child)
    return found


if __name__ == "__main__":
    main()
