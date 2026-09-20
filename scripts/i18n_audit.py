"""Static i18n guard for Deluxe Sync's user-visible Calibre UI."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIALOGS_DIR = ROOT / "DeluxeSync" / "dialogs"
EXTRACT_SCRIPT = ROOT / "scripts" / "extract_translations.py"
FILES = (
    ROOT / "DeluxeSync" / "action.py",
    ROOT / "DeluxeSync" / "config.py",
    *sorted(
        path
        for path in DIALOGS_DIR.glob("*.py")
        if path.name != "__init__.py"
    ),
)

UI_CONSTRUCTORS = {"QLabel", "QPushButton", "QGroupBox", "QCheckBox"}
UI_METHODS = {
    "setWindowTitle",
    "setText",
    "setToolTip",
    "setPlaceholderText",
}
TRANSLATORS = {"_", "ngettext"}


def call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def translated(node: ast.AST) -> bool:
    if isinstance(node, ast.Call) and call_name(node.func) in TRANSLATORS:
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return translated(node.func.value)
    return False


def contains_words(value: str) -> bool:
    return any(character.isalpha() for character in value)


def literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return contains_words(node.value)
    if isinstance(node, ast.JoinedStr):
        return any(
            isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and contains_words(value.value)
            for value in node.values
        )
    return False


def check_argument(
    errors: list[str],
    path: Path,
    node: ast.Call,
    argument: ast.AST | None,
    label: str,
) -> None:
    if argument is not None and literal(argument) and not translated(argument):
        errors.append(
            f"{path.relative_to(ROOT)}:{node.lineno}: "
            f"{label} contains an untranslated user-visible literal"
        )


def main() -> int:
    errors: list[str] = []
    for path in FILES:
        source = path.read_text(encoding="utf-8")
        if "load_translations()" not in source:
            errors.append(
                f"{path.relative_to(ROOT)}: missing load_translations()"
            )
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = call_name(node.func)
            if name in UI_CONSTRUCTORS:
                argument = node.args[0] if node.args else None
                check_argument(errors, path, node, argument, name)
            elif name in UI_METHODS:
                argument = node.args[0] if node.args else None
                check_argument(errors, path, node, argument, name)
            elif name == "create_menu_action":
                text_arg = node.args[2] if len(node.args) > 2 else None
                check_argument(errors, path, node, text_arg, "menu text")
                for keyword in node.keywords:
                    if keyword.arg == "description":
                        check_argument(
                            errors,
                            path,
                            node,
                            keyword.value,
                            "menu description",
                        )

    pot_check = subprocess.run(
        [sys.executable, str(EXTRACT_SCRIPT), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if pot_check.returncode != 0:
        detail = (pot_check.stdout or pot_check.stderr).strip()
        errors.append(detail or "translation template is out of date")

    if errors:
        print("Deluxe Sync i18n audit failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print("Deluxe Sync i18n audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
