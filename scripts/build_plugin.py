"""Build the Deluxe Sync Calibre plugin ZIP."""

from __future__ import annotations

import ast
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "DeluxeSync"
DIST_DIR = ROOT / "dist"
INIT_FILE = SOURCE_DIR / "__init__.py"
IMPORT_MARKER = "plugin-import-name-deluxe_sync.txt"
PLUGIN_ICON = SOURCE_DIR / "icon.png"


def read_version() -> str:
    tree = ast.parse(INIT_FILE.read_text(encoding="utf-8"), filename=str(INIT_FILE))
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
    raise RuntimeError("Could not find DeluxeSyncPlugin.version in DeluxeSync/__init__.py")


def build() -> Path:
    version = read_version()
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    output = DIST_DIR / f"Deluxe_Sync_v{version}.zip"

    source_files = sorted(SOURCE_DIR.rglob("*.py"))
    translation_files = sorted((SOURCE_DIR / "translations").glob("*.mo"))
    required = {
        "__init__.py",
        "action.py",
        "api.py",
        "config.py",
        "logger.py",
        "settings.py",
        "dialogs/__init__.py",
        "dialogs/connection.py",
        "dialogs/columns.py",
    }
    found = {
        path.relative_to(SOURCE_DIR).as_posix()
        for path in source_files
    }
    missing = required - found
    if missing:
        raise RuntimeError(f"Missing required plugin files: {', '.join(sorted(missing))}")
    if not PLUGIN_ICON.is_file():
        raise RuntimeError(f"Missing plugin icon: {PLUGIN_ICON}")

    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for source in source_files:
            archive.write(
                source,
                arcname=source.relative_to(SOURCE_DIR).as_posix(),
            )
        for source in translation_files:
            archive.write(
                source,
                arcname=source.relative_to(SOURCE_DIR).as_posix(),
            )
        archive.write(PLUGIN_ICON, arcname="icon.png")
        archive.writestr(IMPORT_MARKER, "")

    return output


if __name__ == "__main__":
    built = build()
    print(built)
