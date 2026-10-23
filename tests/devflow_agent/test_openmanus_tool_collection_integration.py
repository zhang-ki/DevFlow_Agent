from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from devflow_agent.openmanus_tools import create_openmanus_readonly_collection
from devflow_agent.repository import AuthorizedWorkspace, RepoError


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "repos" / "readonly_demo"
TOOL_COLLECTION_SOURCE = REPOSITORY_ROOT / "app" / "tool" / "tool_collection.py"


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def run_immediate(coroutine):
    """Drive these no-I/O tool coroutines without creating an event-loop socketpair."""
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    finally:
        coroutine.close()
    raise AssertionError("read-only ToolCollection call unexpectedly suspended")


@contextmanager
def load_fixed_tool_collection() -> Iterator[type]:
    """Load the exact upstream ToolCollection source behind a minimal import seam.

    app.tool.__init__ eagerly imports unrelated tools and app.tool.base requires
    Pydantic, which is absent from this offline environment. The production
    ToolCollection source itself only needs the exception, logger and base-type
    names below. This seam executes that exact source file while avoiding all
    default tool imports and optional runtime dependencies.
    """
    prefix_names = {
        name for name in sys.modules if name == "app" or name.startswith("app.")
    }
    previous = {name: sys.modules[name] for name in prefix_names}
    for name in prefix_names:
        del sys.modules[name]

    app_package = types.ModuleType("app")
    app_package.__path__ = [str(REPOSITORY_ROOT / "app")]
    tool_package = types.ModuleType("app.tool")
    tool_package.__path__ = [str(REPOSITORY_ROOT / "app" / "tool")]
    sys.modules["app"] = app_package
    sys.modules["app.tool"] = tool_package

    exceptions_spec = importlib.util.spec_from_file_location(
        "app.exceptions", REPOSITORY_ROOT / "app" / "exceptions.py"
    )
    assert exceptions_spec and exceptions_spec.loader
    exceptions_module = importlib.util.module_from_spec(exceptions_spec)
    sys.modules["app.exceptions"] = exceptions_module
    exceptions_spec.loader.exec_module(exceptions_module)

    logger_module = types.ModuleType("app.logger")
    logger_module.logger = types.SimpleNamespace(warning=lambda *args, **kwargs: None)
    sys.modules["app.logger"] = logger_module

    base_module = types.ModuleType("app.tool.base")

    class BaseTool:
        pass

    class ToolResult:
        pass

    class ToolFailure(ToolResult):
        def __init__(self, *, error: str):
            self.error = error

    base_module.BaseTool = BaseTool
    base_module.ToolResult = ToolResult
    base_module.ToolFailure = ToolFailure
    sys.modules["app.tool.base"] = base_module

    collection_spec = importlib.util.spec_from_file_location(
        "app.tool.tool_collection", TOOL_COLLECTION_SOURCE
    )
    assert collection_spec and collection_spec.loader
    collection_module = importlib.util.module_from_spec(collection_spec)
    sys.modules["app.tool.tool_collection"] = collection_module
    collection_spec.loader.exec_module(collection_module)

    try:
        yield collection_module.ToolCollection
    finally:
        for name in tuple(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.modules.update(previous)


class OpenManusToolCollectionIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="devflow-openmanus-tools-")
        self.repo = Path(self.temporary.name) / "repo"
        shutil.copytree(FIXTURE_ROOT, self.repo)
        self.before = tree_hashes(self.repo)
        self.workspace = AuthorizedWorkspace(self.repo)

    def tearDown(self) -> None:
        self.assertEqual(tree_hashes(self.repo), self.before)
        self.temporary.cleanup()

    def test_real_fixed_collection_contains_only_two_readonly_tools_and_executes(
        self,
    ) -> None:
        with load_fixed_tool_collection() as tool_collection_type:
            self.assertEqual(
                Path(
                    importlib.util.find_spec("app.tool.tool_collection").origin
                ).resolve(),
                TOOL_COLLECTION_SOURCE.resolve(),
            )
            self.assertEqual(
                tool_collection_type.__module__, "app.tool.tool_collection"
            )

            # Construction and calls must not start processes or create sockets.
            with patch.object(
                subprocess, "Popen", side_effect=AssertionError("subprocess forbidden")
            ) as popen, patch.object(
                socket, "socket", side_effect=AssertionError("network forbidden")
            ) as socket_create:
                collection = create_openmanus_readonly_collection(
                    self.workspace, tool_collection_type
                )

                names = [tool.name for tool in collection.tools]
                self.assertEqual(
                    names,
                    ["repository_search_readonly", "repository_readonly_read"],
                )
                self.assertEqual(len(set(names)), 2)
                schemas = collection.to_params()
                self.assertEqual(
                    {schema["function"]["name"] for schema in schemas}, set(names)
                )
                schemas_by_name = {
                    schema["function"]["name"]: schema["function"] for schema in schemas
                }
                for name, tool in collection.tool_map.items():
                    self.assertEqual(schemas_by_name[name], tool.to_param()["function"])
                    self.assertFalse(
                        schemas_by_name[name]["parameters"]["additionalProperties"]
                    )
                self.assertFalse(
                    {"bash", "python_execute", "str_replace_editor", "mcp"} & set(names)
                )
                self.assertEqual(
                    schemas_by_name["repository_search_readonly"]["parameters"][
                        "required"
                    ],
                    ["query"],
                )
                self.assertEqual(
                    schemas_by_name["repository_readonly_read"]["parameters"][
                        "required"
                    ],
                    ["path"],
                )

                search_output = run_immediate(
                    collection.execute(
                        name="repository_search_readonly",
                        tool_input={"query": "sum numeric values", "limit": 5},
                    )
                )
                search = json.loads(search_output)
                self.assertEqual(
                    search["result_kind"], "index_candidates_not_read_evidence"
                )
                self.assertIn(
                    "src/totals.py", {hit["relative_path"] for hit in search["results"]}
                )
                self.assertTrue(
                    all(not hit["read_evidence"] for hit in search["results"])
                )

                source = self.repo / "src" / "totals.py"
                full_hash = hashlib.sha256(source.read_bytes()).hexdigest()
                read_output = run_immediate(
                    collection.execute(
                        name="repository_readonly_read",
                        tool_input={
                            "path": "src/totals.py",
                            "start_line": 1,
                            "end_line": 5,
                        },
                    )
                )
                read_result = json.loads(read_output)
                self.assertEqual(read_result["result_kind"], "trusted_file_read")
                self.assertEqual(read_result["relative_path"], "src/totals.py")
                self.assertEqual(read_result["start_line"], 1)
                self.assertEqual(read_result["end_line"], 5)
                self.assertEqual(read_result["full_file_sha256"], full_hash)
                self.assertIn("def total", read_result["content"])

                bad_args = run_immediate(
                    collection.execute(
                        name="repository_search_readonly",
                        tool_input={"query": "total", "unexpected": True},
                    )
                )
                self.assertEqual(
                    json.loads(bad_args)["error"], "invalid_tool_arguments"
                )
                traversal = run_immediate(
                    collection.execute(
                        name="repository_readonly_read",
                        tool_input={
                            "path": "../outside.txt",
                            "start_line": 1,
                            "end_line": 1,
                        },
                    )
                )
                self.assertEqual(json.loads(traversal)["error"], "path_outside_root")

                popen.assert_not_called()
                socket_create.assert_not_called()

            for dangerous_module in (
                "app.tool.bash",
                "app.tool.python_execute",
                "app.tool.str_replace_editor",
                "app.tool.mcp",
                "app.tool.web_search",
            ):
                self.assertNotIn(dangerous_module, sys.modules)

    def test_nonempty_or_non_openmanus_factory_fails_closed(self) -> None:
        with load_fixed_tool_collection() as tool_collection_type:
            existing = tool_collection_type()
            existing.add_tools(types.SimpleNamespace(name="bash"))
            with self.assertRaises(RepoError):
                create_openmanus_readonly_collection(self.workspace, lambda: existing)
            self.assertEqual([tool.name for tool in existing.tools], ["bash"])

            with self.assertRaises(RepoError):
                create_openmanus_readonly_collection(self.workspace, lambda: object())


if __name__ == "__main__":
    unittest.main()
