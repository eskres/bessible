"""Guards for per-run keys: nothing writes a key to the process environment or builds a model at import time."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCES = sorted((ROOT / "src" / "bessible").rglob("*.py"))
MODEL_BUILDERS = {"gemini_model", "run_model", "developer_model", "GoogleModel", "GoogleProvider"}
ENV_WRITERS = {"putenv", "setdefault", "update", "pop", "clear"}
DEVELOPER_ONLY = {"src/bessible/llm.py", "src/bessible/possibility/__main__.py"}  # plus scripts/


def name_of(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def is_os_environ(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "environ" and name_of(node.value) == "os"


def import_time_nodes(tree: ast.Module) -> list[ast.AST]:
    """Every node that runs on import: module and class bodies, but not function or lambda bodies."""
    nodes: list[ast.AST] = []
    pending: list[ast.AST] = list(tree.body)
    while pending:
        node = pending.pop()
        nodes.append(node)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            pending.extend(node.decorator_list)
            pending.extend(node.args.defaults)
            continue
        if isinstance(node, ast.Lambda):
            continue
        pending.extend(ast.iter_child_nodes(node))
    return nodes


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_model_is_built_at_import_time(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    built = [
        f"line {node.lineno}: {name_of(node.func)}()"
        for node in import_time_nodes(tree)
        if isinstance(node, ast.Call) and name_of(node.func) in MODEL_BUILDERS
    ]
    assert built == []


@pytest.mark.parametrize("path", [*SOURCES, *sorted((ROOT / "scripts").glob("*.py"))], ids=lambda p: p.name)
def test_nothing_writes_to_os_environ(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    writes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AugAssign | ast.Delete):
            targets = node.targets if hasattr(node, "targets") else [node.target]
            writes += [t.lineno for t in targets if isinstance(t, ast.Subscript) and is_os_environ(t.value)]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if is_os_environ(node.func.value) and node.func.attr in ENV_WRITERS:
                writes.append(node.lineno)
            if name_of(node.func.value) == "os" and node.func.attr in {"putenv", "unsetenv"}:
                writes.append(node.lineno)
    assert writes == []


def test_developer_key_is_never_used_by_the_pipeline():
    users = [
        str(p.relative_to(ROOT))
        for p in SOURCES
        if "developer_model" in p.read_text(encoding="utf-8")
        or "settings.google_api_key" in p.read_text(encoding="utf-8")
    ]
    # keystore.py seeds the local user's key from .env only when sign-in is off (AUTH_ENABLED=false, local dev).
    assert set(users) <= DEVELOPER_ONLY | {"src/bessible/cli.py", "src/bessible/config.py", "src/bessible/keystore.py"}
