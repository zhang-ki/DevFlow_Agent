from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devflow_agent.repository import (
    AuthorizedWorkspace,
    RepoError,
    RepoErrorCode,
    WorkspaceLimits,
    build_index,
)
from devflow_agent.tools import register_read_only_tools


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "repos" / "readonly_demo"


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


class ReadonlyRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="devflow-readonly-")
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        shutil.copytree(FIXTURE_ROOT, self.repo)
        self.workspace = AuthorizedWorkspace(self.repo)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_index_paths_python_symbols_and_lines(self) -> None:
        index = build_index(self.workspace)
        paths = {record.relative_path for record in index.file_records}
        self.assertEqual(
            paths,
            {"README.md", "src/totals.py", "tests/test_totals.py"},
        )
        found = [symbol for symbol in index.symbols if symbol.name == "total"]
        self.assertEqual(len(found), 1)
        source_symbol = next(item for item in found if item.relative_path == "src/totals.py")
        self.assertEqual(source_symbol.start_line, 3)
        self.assertEqual(source_symbol.end_line, 5)
        self.assertTrue(all(item.start_line >= 1 for item in index.symbols))

    def test_keyword_search_has_stable_ranking_and_explicit_candidate_state(self) -> None:
        index = build_index(self.workspace)
        first = index.search("sum numeric values", limit=10)
        second = index.search("sum numeric values", limit=10)
        self.assertEqual(first, second)
        self.assertTrue(first)
        self.assertEqual(first[0].relative_path, "src/totals.py")
        self.assertTrue(all(hit.read_evidence is False for hit in first))
        self.assertTrue(all(hit.indexed_sha256 for hit in first))
        self.assertTrue(all(hit.matched_line_numbers for hit in first))
        total_hit = next(hit for hit in first if hit.relative_path == "src/totals.py")
        self.assertEqual(total_hit.line_count, 10)
        self.assertEqual(total_hit.line_count, len((self.repo / "src" / "totals.py").read_text(encoding="utf-8").splitlines()))

    def test_path_and_symbol_filters(self) -> None:
        index = build_index(self.workspace)
        hits = index.search(
            "add values",
            path_filter="src/",
            symbol_filter="RunningTotal.add",
            limit=5,
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].relative_path, "src/totals.py")
        self.assertIn("RunningTotal.add", hits[0].matched_symbols)

    def test_read_returns_actual_full_and_fragment_hashes(self) -> None:
        path = self.repo / "src" / "totals.py"
        raw = path.read_bytes()
        evidence = self.workspace.read_text(
            "src\\totals.py", start_line=3, end_line=5, task_id="task-a", run_id="run-a"
        )
        expected_fragment = b"".join(raw.splitlines(keepends=True)[2:5])
        self.assertEqual(evidence.relative_path, "src/totals.py")
        self.assertEqual(evidence.start_line, 3)
        self.assertEqual(evidence.end_line, 5)
        self.assertEqual(evidence.full_file_sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(evidence.fragment_sha256, hashlib.sha256(expected_fragment).hexdigest())
        self.assertEqual(evidence.content.encode("utf-8"), expected_fragment)
        self.assertEqual(evidence.evidence_kind, "trusted_file_read")
        self.assertEqual(evidence.workspace_id, self.workspace.workspace_id)
        self.assertTrue(evidence.evidence_id)
        self.assertEqual(evidence.task_id, "task-a")
        self.assertEqual(evidence.run_id, "run-a")

    def test_omitted_read_range_returns_bounded_preview_and_actual_line_count(self) -> None:
        source = self.repo / "src" / "paged.py"
        source.write_bytes("".join(f"line-{number}\n" for number in range(1, 9)).encode("utf-8"))
        workspace = AuthorizedWorkspace(
            self.repo,
            limits=WorkspaceLimits(max_read_lines=3, max_return_bytes=100),
        )

        evidence = workspace.read_text("src/paged.py")

        self.assertEqual((evidence.start_line, evidence.end_line), (1, 3))
        self.assertEqual(evidence.line_count, 8)
        self.assertTrue(evidence.truncated)
        self.assertEqual(evidence.content, "line-1\nline-2\nline-3\n")

        byte_limited = AuthorizedWorkspace(
            self.repo,
            limits=WorkspaceLimits(max_read_lines=20, max_return_bytes=12),
        ).read_text("src/paged.py")
        self.assertLessEqual(len(byte_limited.content.encode("utf-8")), 12)
        self.assertTrue(byte_limited.truncated)
        self.assertEqual(byte_limited.line_count, 8)

    def test_explicit_invalid_range_is_rejected_with_actual_line_count(self) -> None:
        with self.assertRaises(RepoError) as raised:
            self.workspace.read_text("src/totals.py", start_line=1, end_line=20)
        self.assertEqual(raised.exception.code, RepoErrorCode.INVALID_LINE_RANGE)
        self.assertEqual(raised.exception.details["line_count"], 10)
        self.assertEqual(raised.exception.details["max_lines"], 500)

    def test_path_traversal_and_absolute_paths_are_rejected(self) -> None:
        for path in ("../outside.py", "src/../../outside.py", str(self.base / "outside.py"), r"C:\outside.py"):
            with self.subTest(path=path):
                with self.assertRaises(RepoError) as raised:
                    self.workspace.read_text(path, start_line=1, end_line=1)
                self.assertIn(
                    raised.exception.code,
                    {RepoErrorCode.PATH_OUTSIDE_ROOT, RepoErrorCode.INVALID_PATH},
                )

    def test_resolved_link_escape_guard_without_os_link_privilege(self) -> None:
        outside = self.base / "outside.py"
        outside.write_text("def outside():\n    return True\n", encoding="utf-8")
        link_dir = self.repo / "src" / "escape"
        link_dir.mkdir()
        target = link_dir / "outside.py"
        resolved_outside = outside.resolve()
        real_resolve = Path.resolve
        real_is_symlink = Path.is_symlink

        def controlled_resolve(path, *args, **kwargs):
            if path == target:
                return resolved_outside
            return real_resolve(path, *args, **kwargs)

        def controlled_is_symlink(path):
            if path == link_dir:
                return True
            return real_is_symlink(path)

        with patch.object(Path, "resolve", controlled_resolve), patch.object(
            Path, "is_symlink", controlled_is_symlink
        ):
            with self.assertRaises(RepoError) as raised:
                self.workspace.read_text("src/escape/outside.py", start_line=1, end_line=1)
        self.assertEqual(raised.exception.code, RepoErrorCode.SYMLINK_ESCAPE)

    def test_unicode_line_numbers_match_read_fragments(self) -> None:
        path = self.repo / "src" / "unicode.txt"
        path.write_text("first\u2028second\n", encoding="utf-8")
        workspace = AuthorizedWorkspace(self.repo)
        index = build_index(workspace)
        record = next(item for item in index.file_records if item.relative_path == "src/unicode.txt")
        self.assertEqual(record.line_count, 2)
        hit = next(item for item in index.search("second") if item.relative_path == "src/unicode.txt")
        self.assertEqual(hit.matched_line_numbers, (2,))
        evidence = workspace.read_text("src/unicode.txt", start_line=1, end_line=1)
        self.assertEqual(evidence.content, "first\u2028")
        self.assertEqual(
            evidence.fragment_sha256,
            hashlib.sha256("first\u2028".encode("utf-8")).hexdigest(),
        )

    def test_total_index_bytes_are_bounded(self) -> None:
        workspace = AuthorizedWorkspace(
            self.repo,
            limits=WorkspaceLimits(max_total_index_bytes=100),
        )
        index = build_index(workspace)
        self.assertLessEqual(index.stats.indexed_bytes, 100)
        self.assertTrue(index.stats.truncated)
    def test_file_scan_count_is_bounded(self) -> None:
        workspace = AuthorizedWorkspace(self.repo, limits=WorkspaceLimits(max_files=2))
        index = build_index(workspace)
        self.assertEqual(index.stats.scanned_files, 2)
        self.assertEqual(index.stats.indexed_files, 2)
        self.assertTrue(index.stats.truncated)
    def test_symlink_or_junction_escape_is_rejected(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "outside.py").write_text("def outside():\n    return True\n", encoding="utf-8")
        link = self.repo / "src" / "escape"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("host does not permit creating symlinks")
            command = f'mklink /J "{link}" "{outside}"'
            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", command],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if completed.returncode != 0:
                self.skipTest("host does not permit creating a temporary directory junction")
        with self.assertRaises(RepoError) as raised:
            self.workspace.read_text("src/escape/outside.py", start_line=1, end_line=2)
        self.assertEqual(raised.exception.code, RepoErrorCode.SYMLINK_ESCAPE)

    def test_sensitive_binary_and_large_files_are_excluded(self) -> None:
        (self.repo / ".env").write_text("FAKE_TEST_VALUE=not-a-secret", encoding="utf-8")
        config = self.repo / "config"
        config.mkdir()
        (config / "config.toml").write_text('api_key = "not-real"', encoding="utf-8")
        models = self.repo / "models"
        models.mkdir()
        (models / "weights.py").write_text("def hidden(): pass\n", encoding="utf-8")
        virtualenv = self.repo / ".venv" / "Lib"
        virtualenv.mkdir(parents=True)
        (virtualenv / "cached.py").write_text("def cached(): pass\n", encoding="utf-8")
        (self.repo / "image.png").write_bytes(b"\x89PNG\x00binary")
        (self.repo / "large.py").write_bytes(b"x" * 300)

        workspace = AuthorizedWorkspace(
            self.repo,
            limits=WorkspaceLimits(max_file_bytes=128, max_total_index_bytes=1024),
        )
        index = build_index(workspace)
        paths = {record.relative_path for record in index.file_records}
        self.assertNotIn(".env", paths)
        self.assertNotIn("config/config.toml", paths)
        self.assertNotIn("models/weights.py", paths)
        self.assertNotIn(".venv/Lib/cached.py", paths)
        self.assertNotIn("image.png", paths)
        self.assertNotIn("large.py", paths)
        self.assertGreaterEqual(index.stats.skipped_by_reason.get("file_too_large", 0), 1)
        with self.assertRaises(RepoError) as raised:
            workspace.read_text(".env", start_line=1, end_line=1)
        self.assertEqual(raised.exception.code, RepoErrorCode.UNSUPPORTED_FILE)

    def test_file_change_after_fragment_read_is_rejected_as_stale(self) -> None:
        target = self.repo / "src" / "totals.py"
        original_bytes = target.read_bytes()
        stable_read = self.workspace._read_stable_bytes
        calls = 0

        def change_before_final_check(relative_path: str):
            nonlocal calls
            calls += 1
            if calls == 2:
                target.write_bytes(b"def total(values):\n    return 99\n")
            return stable_read(relative_path)

        try:
            with patch.object(
                self.workspace,
                "_read_stable_bytes",
                side_effect=change_before_final_check,
            ):
                with self.assertRaises(RepoError) as raised:
                    self.workspace.read_text("src/totals.py", start_line=1, end_line=2)
            self.assertEqual(raised.exception.code, RepoErrorCode.STALE_EVIDENCE)
            self.assertEqual(raised.exception.details["reason"], "post_read_digest_mismatch")
        finally:
            target.write_bytes(original_bytes)

    def test_changed_index_candidate_is_rejected_as_stale(self) -> None:
        index = build_index(self.workspace)
        record = next(item for item in index.file_records if item.relative_path == "src/totals.py")
        (self.repo / "src" / "totals.py").write_text("def total(values):\n    return 99\n", encoding="utf-8")
        with self.assertRaises(RepoError) as raised:
            self.workspace.read_text(
                "src/totals.py",
                start_line=1,
                end_line=2,
                expected_sha256=record.sha256,
            )
        self.assertEqual(raised.exception.code, RepoErrorCode.STALE_EVIDENCE)
        self.assertEqual(raised.exception.details["reason"], "index_digest_mismatch")

    def test_scan_search_read_do_not_mutate_authorized_repository(self) -> None:
        before = tree_hashes(self.repo)
        index = build_index(self.workspace)
        self.assertTrue(index.search("total"))
        self.workspace.read_text("README.md", start_line=1, end_line=2)
        after = tree_hashes(self.repo)
        self.assertEqual(before, after)

    def test_structured_errors_and_limits(self) -> None:
        with self.assertRaises(RepoError) as raised:
            self.workspace.read_text("missing.py", start_line=1, end_line=1)
        self.assertEqual(raised.exception.as_dict()["error"], "not_found")
        with self.assertRaises(RepoError) as raised:
            build_index(self.workspace).search("total", limit=101)
        self.assertEqual(raised.exception.code, RepoErrorCode.INVALID_LIMIT)

    def test_openmanus_read_tool_schema_and_default_preview_are_bounded(self) -> None:
        class Registry:
            def __init__(self) -> None:
                self.tools = []

            def add_tools(self, *tools: object) -> None:
                self.tools.extend(tools)

        registry = Registry()
        search_tool, read_tool = register_read_only_tools(registry, self.workspace)
        self.assertEqual(read_tool.parameters["required"], ["path"])
        self.assertIn("1-based", read_tool.description)
        self.assertIn("bounded preview", read_tool.description)

        search = asyncio.run(search_tool.execute(query="total"))
        total_hit = next(item for item in search["results"] if item["relative_path"] == "src/totals.py")
        self.assertEqual(total_hit["line_count"], 10)
        self.assertEqual(total_hit["line_count_source"], "fresh_index_snapshot")
        self.assertFalse(total_hit["read_evidence"])

        result = asyncio.run(read_tool.execute(path="src/totals.py"))
        self.assertEqual(result["range_mode"], "bounded_default")
        self.assertEqual(result["line_count"], 10)
        self.assertFalse(result["truncated"])
        self.assertEqual((result["start_line"], result["end_line"]), (1, 10))

    def test_openmanus_adapter_registers_only_read_tools_without_importing_app(self) -> None:
        class Registry:
            def __init__(self) -> None:
                self.tools = []

            def add_tools(self, *tools: object) -> None:
                self.tools.extend(tools)

        unsafe_registry = Registry()
        unsafe_registry.tools.append(type("UnsafeTool", (), {"name": "bash"})())
        with self.assertRaises(RepoError) as raised:
            register_read_only_tools(unsafe_registry, self.workspace)
        self.assertEqual(raised.exception.code, RepoErrorCode.UNSAFE_REGISTRY_PROFILE)
        self.assertEqual(len(unsafe_registry.tools), 1)

        registry = Registry()
        search_tool, read_tool = register_read_only_tools(registry, self.workspace)
        self.assertEqual(
            [tool.name for tool in registry.tools],
            ["repository_search_readonly", "repository_readonly_read"],
        )
        self.assertEqual(len(search_tool.to_param()["function"]["parameters"]["required"]), 1)
        isolated_import_probe = r"""
import sys
import tempfile
from pathlib import Path
from devflow_agent.repository import AuthorizedWorkspace
from devflow_agent.tools import register_read_only_tools
class Registry:
    def __init__(self): self.tools = []
    def add_tools(self, *tools): self.tools.extend(tools)
with tempfile.TemporaryDirectory() as directory:
    Path(directory, "README.md").write_text("read only", encoding="utf-8")
    registry = Registry()
    register_read_only_tools(registry, AuthorizedWorkspace(Path(directory)))
    assert [tool.name for tool in registry.tools] == ["repository_search_readonly", "repository_readonly_read"]
assert "app.agent" not in sys.modules
"""
        probe_env = os.environ.copy()
        probe_env["PYTHONPATH"] = str(REPOSITORY_ROOT)
        probe = subprocess.run(
            [sys.executable, "-c", isolated_import_probe],
            cwd=REPOSITORY_ROOT,
            env=probe_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)


        def drive_without_event_loop(coroutine):
            try:
                coroutine.send(None)
            except StopIteration as completed:
                return completed.value
            raise AssertionError("read-only adapters must complete without suspending")

        search_result = json.loads(drive_without_event_loop(search_tool(query="total")))
        self.assertEqual(search_result["result_kind"], "index_candidates_not_read_evidence")
        self.assertTrue(all(item["read_evidence"] is False for item in search_result["results"]))
        read_result = json.loads(
            drive_without_event_loop(read_tool(path="README.md", start_line=1, end_line=1))
        )
        self.assertEqual(read_result["result_kind"], "trusted_file_read")
        self.assertEqual(read_result["relative_path"], "README.md")


class ReadonlyCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="devflow-cli-")
        self.repo = Path(self.temporary.name) / "repo"
        shutil.copytree(FIXTURE_ROOT, self.repo)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "devflow_agent.cli", *arguments],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def test_help_index_search_read(self) -> None:
        before = tree_hashes(self.repo)
        help_result = self.run_cli("--help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("read", help_result.stdout)
        self.assertIn("ask", help_result.stdout)

        index_result = self.run_cli("index", "--repo", str(self.repo), "--limit", "10")
        self.assertEqual(index_result.returncode, 0, index_result.stderr)
        index_json = json.loads(index_result.stdout)
        self.assertEqual(index_json["kind"], "in_memory_index_summary")
        self.assertFalse(index_json["read_evidence"])

        search_result = self.run_cli(
            "search",
            "--repo",
            str(self.repo),
            "--query",
            "total",
            "--symbol",
            "total",
            "--limit",
            "5",
        )
        self.assertEqual(search_result.returncode, 0, search_result.stderr)
        search_json = json.loads(search_result.stdout)
        self.assertTrue(search_json["results"])
        self.assertFalse(search_json["read_evidence"])

        read_result = self.run_cli(
            "read",
            "--repo",
            str(self.repo),
            "--path",
            "src/totals.py",
            "--start-line",
            "3",
            "--end-line",
            "5",
        )
        self.assertEqual(read_result.returncode, 0, read_result.stderr)
        read_json = json.loads(read_result.stdout)
        self.assertEqual(read_json["relative_path"], "src/totals.py")
        self.assertEqual(read_json["start_line"], 3)
        self.assertTrue(read_json["full_file_sha256"])
        self.assertTrue(read_json["fragment_sha256"])
        self.assertNotIn(str(self.repo), read_result.stdout)
        self.assertEqual(before, tree_hashes(self.repo))

    def test_cli_errors_are_machine_readable_and_nonzero(self) -> None:
        result = self.run_cli(
            "read",
            "--repo",
            str(self.repo),
            "--path",
            "../outside.py",
            "--start-line",
            "1",
            "--end-line",
            "1",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error"], "path_outside_root")


if __name__ == "__main__":
    unittest.main()
