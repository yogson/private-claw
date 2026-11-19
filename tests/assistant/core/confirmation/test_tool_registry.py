"""Registry completeness: every confirmation-creating tool is in CONFIRMATION_TOOL_NAMES.

unresolved_pending_ids() only trusts ``pending_confirmation`` results of the tools in
CONFIRMATION_TOOL_NAMES. A new tool that creates confirmations but is not registered
works until a restart or a /stop, after which its proposals are never reconciled and
the agent waits forever. These tests find such tools from the source, so forgetting
to register one fails here instead.
"""

import ast
import inspect
from importlib import import_module
from pathlib import Path

import pytest
import yaml
from pydantic_ai import Tool

import assistant
from assistant.agent.deps import TurnDeps
from assistant.core.confirmation import CONFIRMATION_TOOL_NAMES
from assistant.core.confirmation.history import PENDING_TOOL_RESULT_STATUS

SRC_ROOT = Path(assistant.__file__).resolve().parent
TOOLS_YAML = SRC_ROOT.parents[1] / "config" / "tools.yaml"
REQUESTER_ATTR = "confirmation_requester"


def _requester_users(tree: ast.Module) -> set[str]:
    """Names of the top-level functions in ``tree`` that read ``<x>.confirmation_requester``."""
    users: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if any(
            isinstance(sub, ast.Attribute) and sub.attr == REQUESTER_ATTR for sub in ast.walk(node)
        ):
            users.add(node.name)
    return users


def _module_requester_uses() -> dict[str, set[str]]:
    """Every src module that reads ``.confirmation_requester``, mapped to the using functions.

    TurnDeps declares the field (an annotation, not an attribute read) and the
    orchestrator passes it as a keyword, so neither shows up here.
    """
    uses: dict[str, set[str]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if REQUESTER_ATTR not in source:
            continue
        tree = ast.parse(source)
        has_attr_read = any(
            isinstance(node, ast.Attribute) and node.attr == REQUESTER_ATTR
            for node in ast.walk(tree)
        )
        if not has_attr_read:
            continue
        module = ".".join(path.relative_to(SRC_ROOT.parent).with_suffix("").parts)
        uses[module] = _requester_users(tree)
    return uses


def _tool_entrypoints() -> dict[str, tuple[str, str]]:
    """``"module:attr"`` entrypoints of config/tools.yaml, mapped to (module, attr)."""
    data = yaml.safe_load(TOOLS_YAML.read_text(encoding="utf-8"))
    entries: dict[str, tuple[str, str]] = {}
    for tool in data["tools"]:
        module, attr = (part.strip() for part in tool["entrypoint"].split(":", 1))
        entries[tool["entrypoint"]] = (module, attr)
    return entries


def _runtime_tool_name(module: str, attr: str) -> str:
    """The name the agent (and the session's tool results) uses for this entrypoint."""
    func = getattr(import_module(module), attr)
    return Tool[TurnDeps](func).name


def _returns_pending_status(func_source: str) -> bool:
    tree = ast.parse(func_source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == PENDING_TOOL_RESULT_STATUS:
            return True
        if isinstance(node, ast.Name) and node.id == "PENDING_TOOL_RESULT_STATUS":
            return True
    return False


def test_requester_discovery_finds_the_memory_tool() -> None:
    # Guards the discovery itself: if it found nothing, the checks below would be vacuous.
    uses = _module_requester_uses()
    assert uses.get("assistant.agent.tools.memory_propose_update") == {"memory_propose_update"}


def test_requester_discovery_finds_the_capability_edit_tools() -> None:
    uses = _module_requester_uses()
    assert uses.get("assistant.agent.tools.capability_edit") == {
        "capability_propose_edit",
        "capability_propose_rollback",
    }
    assert {"capability_propose_edit", "capability_propose_rollback"} <= CONFIRMATION_TOOL_NAMES
    # Read-only capability tools never create confirmations, so they are not registered.
    assert not {"capability_read", "capability_history"} & CONFIRMATION_TOOL_NAMES


def test_every_requester_user_is_a_registered_confirmation_tool() -> None:
    entrypoints = {(module, attr) for module, attr in _tool_entrypoints().values()}
    unregistered: list[str] = []
    for module, functions in _module_requester_uses().items():
        for function in sorted(functions):
            if (module, function) not in entrypoints:
                unregistered.append(
                    f"{module}:{function} uses confirmation_requester but is not a "
                    "config/tools.yaml entrypoint; request confirmations from the tool "
                    "function itself so its name can be registered"
                )
                continue
            name = _runtime_tool_name(module, function)
            if name not in CONFIRMATION_TOOL_NAMES:
                unregistered.append(
                    f"tool {name!r} ({module}:{function}) uses confirmation_requester; "
                    "add it to CONFIRMATION_TOOL_NAMES in assistant.core.confirmation.history"
                )
    assert not unregistered, "\n".join(unregistered)


def test_every_tool_returning_pending_confirmation_is_registered() -> None:
    unregistered: list[str] = []
    for module, attr in _tool_entrypoints().values():
        func = getattr(import_module(module), attr)
        try:
            source = inspect.getsource(func)
        except (OSError, TypeError):
            continue
        if not _returns_pending_status(inspect.cleandoc("\n" + source)):
            continue
        name = _runtime_tool_name(module, attr)
        if name not in CONFIRMATION_TOOL_NAMES:
            unregistered.append(f"{name!r} ({module}:{attr})")
    assert not unregistered, (
        "tools return status 'pending_confirmation' but are not in CONFIRMATION_TOOL_NAMES: "
        + ", ".join(unregistered)
    )


def test_registry_has_no_stale_names() -> None:
    discovered = {
        _runtime_tool_name(module, function)
        for module, functions in _module_requester_uses().items()
        for function in functions
    }
    assert set(CONFIRMATION_TOOL_NAMES) == discovered


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("async def t(ctx):\n    r = ctx.deps.confirmation_requester\n", {"t"}),
        ("async def t(ctx):\n    await ctx.deps.confirmation_requester(d)\n", {"t"}),
        ("def helper(deps):\n    return getattr(deps, 'x')\n", set()),
        ("x = 1\n", set()),
    ],
)
def test_requester_user_detection(source: str, expected: set[str]) -> None:
    assert _requester_users(ast.parse(source)) == expected
