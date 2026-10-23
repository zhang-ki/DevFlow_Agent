"""Lazy public exports for Agent implementations.

Importing a specific Agent should not eagerly import unrelated browser, MCP,
or shell-based profiles and their optional dependencies.
"""

from importlib import import_module
from typing import Any


_EXPORTS = {
    "BaseAgent": ("app.agent.base", "BaseAgent"),
    "BrowserAgent": ("app.agent.browser", "BrowserAgent"),
    "ReActAgent": ("app.agent.react", "ReActAgent"),
    "SWEAgent": ("app.agent.swe", "SWEAgent"),
    "ToolCallAgent": ("app.agent.toolcall", "ToolCallAgent"),
    "MCPAgent": ("app.agent.mcp", "MCPAgent"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
