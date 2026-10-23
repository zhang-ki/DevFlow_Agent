"""Construction helpers for an explicitly read-only OpenManus ToolCollection.

The caller must supply the OpenManus ToolCollection class. This module does not
import app.tool, so importing a read-only profile cannot trigger the upstream
package's eager tool imports. The supplied collection is constructed empty and
is rejected unless the exact two repository-read tools are its complete tool
set.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from .repository import AuthorizedWorkspace, RepoError, RepoErrorCode
from .tools import RepositoryReadTool, RepositorySearchTool, register_read_only_tools


ALLOWED_TOOL_NAMES = frozenset(
    {"repository_search_readonly", "repository_readonly_read"}
)


class ToolCollectionLike(Protocol):
    tools: tuple[Any, ...]
    tool_map: dict[str, Any]

    def add_tools(self, *tools: Any) -> Any:
        ...

    def to_params(self) -> list[dict[str, Any]]:
        ...


def create_openmanus_readonly_collection(
    workspace: AuthorizedWorkspace,
    tool_collection_factory: Callable[[], ToolCollectionLike],
) -> ToolCollectionLike:
    """Build an empty supplied collection and add exactly the two safe tools.

    The factory must be the fixed OpenManus ``ToolCollection`` constructor;
    callers must not pass Manus/SWEAgent/default-tool factories. Runtime checks
    make a nonempty or behaviorally incompatible collection fail closed.
    """

    if not callable(tool_collection_factory):
        raise RepoError(
            RepoErrorCode.UNSAFE_REGISTRY_PROFILE,
            "an explicit empty ToolCollection factory is required",
        )

    collection = tool_collection_factory()
    collection_type = type(collection)
    if (
        collection_type.__name__ != "ToolCollection"
        or collection_type.__module__ != "app.tool.tool_collection"
    ):
        raise RepoError(
            RepoErrorCode.UNSAFE_REGISTRY_PROFILE,
            "factory did not construct the fixed OpenManus ToolCollection",
        )
    register_read_only_tools(collection, workspace)

    tools = tuple(collection.tools)
    names = tuple(getattr(tool, "name", None) for tool in tools)
    if (
        len(tools) != 2
        or len(set(names)) != 2
        or set(names) != ALLOWED_TOOL_NAMES
        or set(collection.tool_map) != ALLOWED_TOOL_NAMES
    ):
        raise RepoError(
            RepoErrorCode.UNSAFE_REGISTRY_PROFILE,
            "collection does not contain exactly the read-only allowlist",
        )

    expected_types = {RepositorySearchTool, RepositoryReadTool}
    if {type(tool) for tool in tools} != expected_types:
        raise RepoError(
            RepoErrorCode.UNSAFE_REGISTRY_PROFILE,
            "collection contains an unexpected tool implementation",
        )

    try:
        parameter_names = {item["function"]["name"] for item in collection.to_params()}
    except (AttributeError, KeyError, TypeError) as exc:
        raise RepoError(
            RepoErrorCode.UNSAFE_REGISTRY_PROFILE,
            "collection does not expose compatible tool parameter schemas",
            cause_type=type(exc).__name__,
        ) from exc
    if parameter_names != ALLOWED_TOOL_NAMES:
        raise RepoError(
            RepoErrorCode.UNSAFE_REGISTRY_PROFILE,
            "collection parameter schemas do not match the read-only allowlist",
        )

    return collection
