"""Deterministic local CLI for read-only repository search and evidence reads."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .diagnostics import MAX_LOG_CHARS, diagnose_failures
from .real_readonly_agent import ReadonlyAgentError, run_local_readonly_question
from .repository import AuthorizedWorkspace, RepoError, RepoErrorCode, build_index


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _emit(value: dict[str, Any], *, max_bytes: int) -> None:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    if len(rendered.encode("utf-8")) > max_bytes:
        raise RepoError(
            RepoErrorCode.RETURN_LIMIT_EXCEEDED,
            "CLI result exceeds the configured output limit",
            details={"max_return_bytes": max_bytes},
        )
    print(rendered)


def _workspace(args: argparse.Namespace) -> AuthorizedWorkspace:
    return AuthorizedWorkspace(Path(args.repo))


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devflow-repo",
        description="Read-only, bounded source search and evidence reads for an authorized local repository.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser(
        "index", help="build an in-memory path and Python symbol index"
    )
    index_parser.add_argument(
        "--repo", required=True, help="explicit authorized repository root"
    )
    index_parser.add_argument(
        "--limit",
        type=_positive_int,
        default=50,
        help="maximum listed paths (default: 50)",
    )

    search_parser = subparsers.add_parser(
        "search", help="search source keywords, paths, and Python symbols"
    )
    search_parser.add_argument(
        "--repo", required=True, help="explicit authorized repository root"
    )
    search_parser.add_argument("--query", required=True, help="keyword or symbol text")
    search_parser.add_argument(
        "--path", dest="path_filter", help="relative path substring filter"
    )
    search_parser.add_argument(
        "--symbol",
        dest="symbol_filter",
        help="Python qualified-symbol substring filter",
    )
    search_parser.add_argument(
        "--limit", type=_positive_int, default=10, help="maximum results (default: 10)"
    )

    read_parser = subparsers.add_parser(
        "read",
        help="read an explicit line range or a bounded default preview from a supported text file",
    )
    read_parser.add_argument(
        "--repo", required=True, help="explicit authorized repository root"
    )
    read_parser.add_argument(
        "--path", required=True, help="workspace-relative file path"
    )
    read_parser.add_argument("--start-line", type=_positive_int, default=1)
    read_parser.add_argument("--end-line", type=_positive_int)
    read_parser.add_argument(
        "--expected-sha256",
        help="reject a candidate if its indexed file version changed",
    )

    ask_parser = subparsers.add_parser(
        "ask", help="ask a bounded real local model to search and read this repository"
    )
    ask_parser.add_argument(
        "--repo", required=True, help="explicit authorized repository root"
    )
    ask_parser.add_argument("--question", required=True, help="repository question")
    ask_parser.add_argument(
        "--max-steps",
        type=_positive_int,
        default=6,
        help="maximum model decisions (1-12)",
    )
    ask_parser.add_argument(
        "--timeout-seconds",
        type=_positive_int,
        default=180,
        help="whole-run timeout (1-300 seconds)",
    )
    ask_parser.add_argument(
        "--max-output-tokens",
        type=_positive_int,
        help="explicit bounded final-answer budget (1-1536); default comes from config/config.toml",
    )

    diagnose_parser = subparsers.add_parser(
        "diagnose",
        help="diagnose an already captured pytest log using read-only repository evidence",
    )
    diagnose_parser.add_argument(
        "--repo", required=True, help="explicit authorized repository root"
    )
    diagnose_parser.add_argument(
        "--log",
        required=True,
        help="path to an existing pytest log; commands in it are never run",
    )
    diagnose_parser.add_argument(
        "--exit-code", type=int, help="pytest exit code captured with the log"
    )
    diagnose_parser.add_argument(
        "--format", choices=("json", "markdown"), default="json"
    )
    diagnose_parser.add_argument(
        "--compact",
        action="store_true",
        help="emit the bounded final-diagnosis contract (at most three evidence items)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _make_parser()
    args = parser.parse_args(argv)
    try:
        workspace = _workspace(args)
        if args.command == "ask":
            result = asyncio.run(
                run_local_readonly_question(
                    workspace,
                    args.question,
                    max_steps=args.max_steps,
                    timeout_seconds=args.timeout_seconds,
                    max_output_tokens=args.max_output_tokens,
                )
            )
            _emit(result, max_bytes=1_048_576)
            return 0 if result.get("status") == "verified" else 3
        if args.command == "diagnose":
            log_path = Path(args.log).expanduser().resolve(strict=True)
            if not log_path.is_file():
                raise RepoError(
                    RepoErrorCode.NOT_A_FILE, "diagnostic log is not a regular file"
                )
            if log_path.stat().st_size > MAX_LOG_CHARS * 4:
                raise RepoError(
                    RepoErrorCode.FILE_TOO_LARGE,
                    "diagnostic log exceeds the bounded input limit",
                    details={"max_bytes": MAX_LOG_CHARS * 4},
                )
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
            report = diagnose_failures(workspace, log_text, exit_code=args.exit_code)
            if args.format == "markdown":
                print(
                    (
                        report.to_compact_markdown()
                        if args.compact
                        else report.to_markdown()
                    ),
                    end="",
                )
            else:
                print(report.to_compact_json() if args.compact else report.to_json())
            return 0 if report.status == "PASS" else 3
        if args.command == "index":
            index = build_index(workspace)
            limit = min(args.limit, workspace.limits.max_search_results)
            _emit(
                {
                    "kind": "in_memory_index_summary",
                    "read_evidence": False,
                    "scanned_files": index.stats.scanned_files,
                    "indexed_files": index.stats.indexed_files,
                    "indexed_symbols": index.stats.indexed_symbols,
                    "indexed_bytes": index.stats.indexed_bytes,
                    "skipped_by_reason": dict(index.stats.skipped_by_reason),
                    "scan_truncated": index.stats.truncated,
                    "listed_files": [
                        asdict(record) for record in index.file_records[:limit]
                    ],
                    "listed_files_truncated": len(index.file_records) > limit,
                },
                max_bytes=workspace.limits.max_return_bytes,
            )
            return 0

        if args.command == "search":
            index = build_index(workspace)
            hits = index.search(
                args.query,
                path_filter=args.path_filter,
                symbol_filter=args.symbol_filter,
                limit=args.limit,
            )
            _emit(
                {
                    "kind": "index_candidates_not_read_evidence",
                    "index_id": index.index_id,
                    "read_evidence": False,
                    "stats": asdict(index.stats),
                    "results": [asdict(hit) for hit in hits],
                },
                max_bytes=workspace.limits.max_return_bytes,
            )
            return 0

        evidence = workspace.read_text(
            args.path,
            start_line=args.start_line,
            end_line=args.end_line,
            expected_sha256=args.expected_sha256,
        )
        _emit(asdict(evidence), max_bytes=workspace.limits.max_return_bytes)
        return 0
    except RepoError as exc:
        print(
            json.dumps(exc.as_dict(), ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    except ReadonlyAgentError as exc:
        print(
            json.dumps(
                {"status": "blocked", **exc.as_dict()},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
