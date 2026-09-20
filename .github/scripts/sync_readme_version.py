#!/usr/bin/env python3
"""Synchronize README's displayed plugin version from DeluxeSync/__init__.py."""

from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INIT_PATH = ROOT / "DeluxeSync" / "__init__.py"
README_PATH = ROOT / "README.md"
README_VERSION_RE = re.compile(
    r"^\*\*Current plugin version: [^*]+\*\*$",
    re.MULTILINE,
)


def read_version() -> str:
    tree = ast.parse(INIT_PATH.read_text(encoding="utf-8"), filename=str(INIT_PATH))
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "DeluxeSyncPlugin":
            continue
        for statement in node.body:
            if not isinstance(statement, ast.Assign):
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == "version"
                for target in statement.targets
            ):
                continue
            value = ast.literal_eval(statement.value)
            if (
                isinstance(value, tuple)
                and value
                and all(isinstance(part, int) for part in value)
            ):
                return ".".join(str(part) for part in value)
    raise SystemExit(f"Could not read DeluxeSyncPlugin.version from {INIT_PATH}")


def main() -> None:
    version = read_version()
    readme = README_PATH.read_text(encoding="utf-8")
    updated, count = README_VERSION_RE.subn(
        f"**Current plugin version: {version}**",
        readme,
        count=1,
    )
    if count != 1:
        raise SystemExit(f"Expected exactly one README version line in {README_PATH}")

    README_PATH.write_text(updated, encoding="utf-8")
    print(version)


if __name__ == "__main__":
    main()
