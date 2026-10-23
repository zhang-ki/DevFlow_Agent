"""Lazy public exports for tools.

Importing a specific read-only tool should not import shell, browser, web
search, MCP, or editing integrations as an incidental package side effect.
The public names remain available and are imported only when requested.
"""

from importlib import import_module
from typing import Any


_EXPORTS = {
    "BaseTool": ("app.tool.base", "BaseTool"),
    "Bash": ("app.tool.bash", "Bash"),
    "Terminate": ("app.tool.terminate", "Terminate"),
    "StrReplaceEditor": ("app.tool.str_replace_editor", "StrReplaceEditor"),
    "WebSearch": ("app.tool.web_search", "WebSearch"),
    "ToolCollection": ("app.tool.tool_collection", "ToolCollection"),
    "CreateChatCompletion": (
        "app.tool.create_chat_completion",
        "CreateChatCompletion",
    ),
    "PlanningTool": ("app.tool.planning", "PlanningTool"),
    "Crawl4aiTool": ("app.tool.crawl4ai", "Crawl4aiTool"),
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
