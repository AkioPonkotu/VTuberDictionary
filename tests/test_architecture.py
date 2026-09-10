"""Dependency rules that keep the application core independent of adapters."""

from __future__ import annotations

import ast
from pathlib import Path

APPLICATION_DIRECTORY = Path(__file__).parents[1] / "src" / "vtuber_dictionary" / "application"


def test_application_does_not_import_outer_layers() -> None:
    """Use cases may depend on domain types and ports, never concrete adapters."""
    violations: list[str] = []
    for path in APPLICATION_DIRECTORY.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "infrastructure" in node.module:
                violations.append(f"{path.name}:{node.lineno}: {node.module}")
            elif isinstance(node, ast.Import):
                violations.extend(
                    f"{path.name}:{node.lineno}: {alias.name}"
                    for alias in node.names
                    if "infrastructure" in alias.name
                )
    assert not violations, "Application layer imports infrastructure:\n" + "\n".join(violations)
