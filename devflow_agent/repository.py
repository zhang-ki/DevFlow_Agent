"""Bounded, deterministic, read-only repository indexing and evidence reads."""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import os
import re
import stat
import uuid
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable, Mapping


class RepoErrorCode(str, Enum):
    ROOT_NOT_FOUND = "root_not_found"
    INVALID_PATH = "invalid_path"
    PATH_OUTSIDE_ROOT = "path_outside_root"
    SYMLINK_ESCAPE = "symlink_escape"
    NOT_FOUND = "not_found"
    NOT_A_FILE = "not_a_file"
    UNSUPPORTED_FILE = "unsupported_file"
    FILE_TOO_LARGE = "file_too_large"
    INVALID_TEXT = "invalid_text"
    INVALID_QUERY = "invalid_query"
    INVALID_LIMIT = "invalid_limit"
    INVALID_LINE_RANGE = "invalid_line_range"
    RETURN_LIMIT_EXCEEDED = "return_limit_exceeded"
    STALE_EVIDENCE = "stale_evidence"
    IO_ERROR = "io_error"
    UNSAFE_REGISTRY_PROFILE = "unsafe_registry_profile"


class RepoError(Exception):
    """Stable error contract; exception messages never include absolute paths."""

    def __init__(
        self,
        code: RepoErrorCode,
        message: str,
        *,
        relative_path: str | None = None,
        cause_type: str | None = None,
        details: Mapping[str, str | int | bool | None] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.relative_path = relative_path
        self.cause_type = cause_type
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, object]:
        value: dict[str, object] = {"error": self.code.value, "message": self.message}
        if self.relative_path is not None:
            value["relative_path"] = self.relative_path
        if self.cause_type is not None:
            value["cause_type"] = self.cause_type
        if self.details:
            value["details"] = self.details
        return value

    def __str__(self) -> str:
        suffix = f" ({self.relative_path})" if self.relative_path else ""
        return f"{self.code.value}: {self.message}{suffix}"


@dataclass(frozen=True)
class WorkspaceLimits:
    max_file_bytes: int = 1_048_576
    max_files: int = 10_000
    max_total_index_bytes: int = 67_108_864
    max_return_bytes: int = 65_536
    max_read_lines: int = 500
    max_search_results: int = 100

    def __post_init__(self) -> None:
        values = (
            self.max_file_bytes,
            self.max_files,
            self.max_total_index_bytes,
            self.max_return_bytes,
            self.max_read_lines,
            self.max_search_results,
        )
        if any(value <= 0 for value in values):
            raise ValueError("workspace limits must be positive")


@dataclass(frozen=True)
class FileRecord:
    relative_path: str
    size_bytes: int
    sha256: str
    line_count: int
    language: str


@dataclass(frozen=True)
class SymbolRecord:
    relative_path: str
    name: str
    qualified_name: str
    kind: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class SearchHit:
    """Index candidate only; it does not prove that a model read the file."""

    relative_path: str
    score: int
    matched_terms: tuple[str, ...]
    matched_line_numbers: tuple[int, ...]
    matched_symbols: tuple[str, ...]
    indexed_sha256: str
    line_count: int
    read_evidence: bool = field(default=False, init=False)


@dataclass(frozen=True)
class ReadEvidence:
    """Content actually read from disk and checked for concurrent changes."""

    evidence_id: str
    workspace_id: str
    relative_path: str
    start_line: int
    end_line: int
    content: str
    full_file_sha256: str
    fragment_sha256: str
    file_size_bytes: int
    line_count: int
    truncated: bool
    task_id: str | None = None
    run_id: str | None = None
    evidence_kind: str = field(default="trusted_file_read", init=False)


_EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    "node_modules",
    "build",
    "dist",
    "target",
    "coverage",
    "artifacts",
    "models",
    "model",
    "weights",
    "checkpoints",
    "runtime",
    "logs",
    "cache",
    ".cache",
    ".codex",
    ".aws",
}
_EXCLUDED_NAMES = {
    ".env",
    "config.toml",
    "config.json",
    "config.yaml",
    "config.yml",
    "config.ini",
    "settings.toml",
    "settings.json",
    "settings.yaml",
    "settings.yml",
    "mcp.json",
    "credentials",
    "credentials.json",
    "credentials.toml",
    "credentials.yaml",
    "credentials.yml",
    "secrets",
    "secrets.json",
    "secrets.toml",
    "secrets.yaml",
    "secrets.yml",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_ed25519",
}
_EXCLUDED_PATTERNS = (
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.kdbx",
    "*.gguf",
    "*.safetensors",
    "*.onnx",
    "*.pt",
    "*.pth",
    "*.ckpt",
    "*.model",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
    "*.exe",
    "*.dll",
    "*.pdb",
    "*.pyc",
    "*.class",
    "*.so",
    "*.dylib",
    "*.bin",
    "*.zip",
    "*.7z",
    "*.tar",
    "*.gz",
)
_SUPPORTED_EXTENSIONS = {
    ".py",
    ".pyi",
    ".md",
    ".rst",
    ".txt",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".jsonl",
    ".ini",
    ".cfg",
    ".conf",
    ".xml",
    ".html",
    ".css",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".go",
    ".rs",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".hpp",
    ".cs",
    ".sql",
    ".sh",
    ".ps1",
    ".bat",
    ".cmd",
    ".csv",
}
_SUPPORTED_NAMES = {
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
    "dockerfile",
    "makefile",
    "procfile",
    "license",
    "notice",
    "requirements.txt",
    "setup.py",
    "pyproject.toml",
}
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[\u3400-\u9fff]+")


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(getattr(info, "st_dev", 0)),
        int(getattr(info, "st_ino", 0)),
        int(info.st_size),
        int(info.st_mtime_ns),
    )


class AuthorizedWorkspace:
    """Read-only access rooted at one explicitly supplied directory."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        limits: WorkspaceLimits | None = None,
    ) -> None:
        try:
            resolved = Path(root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise RepoError(
                RepoErrorCode.ROOT_NOT_FOUND,
                "authorized repository root cannot be resolved",
                cause_type=type(exc).__name__,
            ) from exc
        if not resolved.is_dir():
            raise RepoError(
                RepoErrorCode.ROOT_NOT_FOUND, "repository root is not a directory"
            )
        self.root = resolved
        self.limits = limits or WorkspaceLimits()
        self.workspace_id = str(uuid.uuid4())

    @staticmethod
    def normalize_relative_path(value: str) -> tuple[str, ...]:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise RepoError(
                RepoErrorCode.INVALID_PATH, "a non-empty relative path is required"
            )
        normalized = value.replace("\\", "/")
        win_path = PureWindowsPath(value)
        if normalized.startswith("/") or win_path.is_absolute() or win_path.drive:
            raise RepoError(
                RepoErrorCode.PATH_OUTSIDE_ROOT, "absolute paths are not allowed"
            )
        parts = tuple(part for part in normalized.split("/") if part not in ("", "."))
        if not parts:
            raise RepoError(RepoErrorCode.INVALID_PATH, "path does not name a file")
        if any(part == ".." for part in parts):
            raise RepoError(
                RepoErrorCode.PATH_OUTSIDE_ROOT, "parent traversal is not allowed"
            )
        return parts

    def _contains_link(self, parts: tuple[str, ...]) -> bool:
        current = self.root
        for part in parts:
            current = current / part
            is_junction = getattr(current, "is_junction", lambda: False)
            if current.is_symlink() or is_junction():
                return True
        return False

    def _resolve_existing(self, value: str) -> tuple[Path, str]:
        parts = self.normalize_relative_path(value)
        lexical = self.root.joinpath(*parts)
        try:
            resolved = lexical.resolve(strict=True)
        except FileNotFoundError as exc:
            raise RepoError(
                RepoErrorCode.NOT_FOUND,
                "workspace file was not found",
                relative_path="/".join(parts),
                cause_type=type(exc).__name__,
            ) from exc
        except (OSError, RuntimeError) as exc:
            raise RepoError(
                RepoErrorCode.IO_ERROR,
                "workspace path could not be resolved",
                relative_path="/".join(parts),
                cause_type=type(exc).__name__,
            ) from exc
        try:
            canonical_relative = resolved.relative_to(self.root).as_posix()
        except ValueError as exc:
            code = (
                RepoErrorCode.SYMLINK_ESCAPE
                if self._contains_link(parts)
                else RepoErrorCode.PATH_OUTSIDE_ROOT
            )
            raise RepoError(
                code,
                "resolved path leaves the authorized repository",
                relative_path="/".join(parts),
            ) from exc
        return resolved, canonical_relative

    @staticmethod
    def _excluded_reason(relative_path: str) -> str | None:
        path = PurePosixPath(relative_path)
        if any(part.casefold() in _EXCLUDED_DIRS for part in path.parts[:-1]):
            return "excluded_directory"
        name = path.name.casefold()
        if name in _EXCLUDED_NAMES:
            return "sensitive_name"
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in _EXCLUDED_PATTERNS):
            return "excluded_artifact"
        return None

    @staticmethod
    def _supported_text(relative_path: str) -> bool:
        path = PurePosixPath(relative_path)
        return (
            path.name.casefold() in _SUPPORTED_NAMES
            or path.suffix.casefold() in _SUPPORTED_EXTENSIONS
        )

    def _read_once(
        self, path: Path, relative: str
    ) -> tuple[bytes, tuple[int, int, int, int]]:
        try:
            with path.open("rb") as stream:
                before = os.fstat(stream.fileno())
                before_id = _identity(before)
                if not stat.S_ISREG(before.st_mode):
                    raise RepoError(
                        RepoErrorCode.NOT_A_FILE,
                        "only regular files can be read",
                        relative_path=relative,
                    )
                if before.st_size > self.limits.max_file_bytes:
                    raise RepoError(
                        RepoErrorCode.FILE_TOO_LARGE,
                        "file exceeds the configured read limit",
                        relative_path=relative,
                    )
                raw = stream.read(self.limits.max_file_bytes + 1)
                after = os.fstat(stream.fileno())
        except RepoError:
            raise
        except OSError as exc:
            raise RepoError(
                RepoErrorCode.IO_ERROR,
                "workspace file could not be read",
                relative_path=relative,
                cause_type=type(exc).__name__,
            ) from exc
        after_id = _identity(after)
        if before_id != after_id or len(raw) > self.limits.max_file_bytes:
            raise RepoError(
                RepoErrorCode.STALE_EVIDENCE,
                "file changed while it was being read",
                relative_path=relative,
                details={"reason": "changed_during_read"},
            )
        return raw, after_id

    def _read_stable_bytes(self, relative_path: str) -> tuple[bytes, str]:
        path1, rel1 = self._resolve_existing(relative_path)
        if not path1.is_file():
            raise RepoError(
                RepoErrorCode.NOT_A_FILE,
                "workspace path is not a file",
                relative_path=rel1,
            )
        data1, identity1 = self._read_once(path1, rel1)
        path2, rel2 = self._resolve_existing(relative_path)
        if path1 != path2 or rel1 != rel2:
            raise RepoError(
                RepoErrorCode.STALE_EVIDENCE,
                "workspace path resolved to a different file during verification",
                relative_path=rel1,
                details={"reason": "resolved_path_changed"},
            )
        data2, identity2 = self._read_once(path2, rel2)
        if identity1 != identity2 or data1 != data2:
            raise RepoError(
                RepoErrorCode.STALE_EVIDENCE,
                "file changed during read verification",
                relative_path=rel1,
                details={"reason": "content_or_metadata_changed"},
            )
        return data1, rel1

    def read_text(
        self,
        relative_path: str,
        *,
        start_line: int = 1,
        end_line: int | None = None,
        expected_sha256: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> ReadEvidence:
        path, relative = self._resolve_existing(relative_path)
        reason = self._excluded_reason(relative)
        if reason or not self._supported_text(relative):
            raise RepoError(
                RepoErrorCode.UNSUPPORTED_FILE,
                "file is excluded or not a supported text type",
                relative_path=relative,
                details={"reason": reason or "unsupported_type"},
            )
        raw, relative = self._read_stable_bytes(relative_path)
        if b"\x00" in raw:
            raise RepoError(
                RepoErrorCode.INVALID_TEXT,
                "binary content is not supported",
                relative_path=relative,
            )
        try:
            decoded_text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RepoError(
                RepoErrorCode.INVALID_TEXT,
                "file is not valid UTF-8 text",
                relative_path=relative,
                cause_type=type(exc).__name__,
            ) from exc

        full_hash = hashlib.sha256(raw).hexdigest()
        if (
            expected_sha256 is not None
            and full_hash.casefold() != expected_sha256.casefold()
        ):
            raise RepoError(
                RepoErrorCode.STALE_EVIDENCE,
                "current file digest differs from the indexed candidate",
                relative_path=relative,
                details={"reason": "index_digest_mismatch"},
            )

        lines = (
            [line.encode("utf-8") for line in decoded_text.splitlines(keepends=True)]
            if raw
            else []
        )
        line_count = len(lines)
        if (
            isinstance(start_line, bool)
            or not isinstance(start_line, int)
            or start_line < 1
            or start_line > line_count
        ):
            raise RepoError(
                RepoErrorCode.INVALID_LINE_RANGE,
                "start_line must identify an existing 1-based line",
                relative_path=relative,
                details={
                    "line_count": line_count,
                    "max_lines": self.limits.max_read_lines,
                },
            )

        if end_line is None:
            # An omitted end line means a bounded preview from start_line. The
            # returned range and truncated flag make this explicit to callers.
            final_line = min(line_count, start_line + self.limits.max_read_lines - 1)
            fragment = b"".join(lines[start_line - 1 : final_line])
            while (
                final_line >= start_line
                and len(fragment) > self.limits.max_return_bytes
            ):
                final_line -= 1
                fragment = b"".join(lines[start_line - 1 : final_line])
            if final_line < start_line:
                raise RepoError(
                    RepoErrorCode.RETURN_LIMIT_EXCEEDED,
                    "no complete line fits within the configured response limit",
                    relative_path=relative,
                    details={
                        "max_return_bytes": self.limits.max_return_bytes,
                        "line_count": line_count,
                    },
                )
            truncated = final_line < line_count
        else:
            if (
                isinstance(end_line, bool)
                or not isinstance(end_line, int)
                or end_line < start_line
                or end_line > line_count
                or end_line - start_line + 1 > self.limits.max_read_lines
            ):
                raise RepoError(
                    RepoErrorCode.INVALID_LINE_RANGE,
                    "explicit line range is invalid or exceeds the configured limit",
                    relative_path=relative,
                    details={
                        "line_count": line_count,
                        "max_lines": self.limits.max_read_lines,
                    },
                )
            final_line = end_line
            fragment = b"".join(lines[start_line - 1 : final_line])
            if len(fragment) > self.limits.max_return_bytes:
                raise RepoError(
                    RepoErrorCode.RETURN_LIMIT_EXCEEDED,
                    "requested content exceeds the configured response limit",
                    relative_path=relative,
                    details={"max_return_bytes": self.limits.max_return_bytes},
                )
            truncated = False

        final_raw, final_relative = self._read_stable_bytes(relative_path)
        if (
            final_relative != relative
            or hashlib.sha256(final_raw).hexdigest() != full_hash
        ):
            raise RepoError(
                RepoErrorCode.STALE_EVIDENCE,
                "file changed before read evidence could be issued",
                relative_path=relative,
                details={"reason": "post_read_digest_mismatch"},
            )
        return ReadEvidence(
            evidence_id=str(uuid.uuid4()),
            workspace_id=self.workspace_id,
            relative_path=relative,
            start_line=start_line,
            end_line=final_line,
            content=fragment.decode("utf-8"),
            full_file_sha256=full_hash,
            fragment_sha256=hashlib.sha256(fragment).hexdigest(),
            file_size_bytes=len(raw),
            line_count=line_count,
            truncated=truncated,
            task_id=task_id,
            run_id=run_id,
        )

    def iter_files(self) -> Iterable[str]:
        """Yield eligible relative paths in stable order, without following links."""

        def fail_on_walk_error(error: OSError) -> None:
            raise RepoError(
                RepoErrorCode.IO_ERROR,
                "repository directory could not be scanned",
                cause_type=type(error).__name__,
            ) from error

        for current, directories, filenames in os.walk(
            self.root, followlinks=False, onerror=fail_on_walk_error
        ):
            current_path = Path(current)
            kept: list[str] = []
            for directory in sorted(
                directories, key=lambda item: (item.casefold(), item)
            ):
                candidate = current_path / directory
                relative = candidate.relative_to(self.root).as_posix()
                is_junction = getattr(candidate, "is_junction", lambda: False)
                if (
                    candidate.is_symlink()
                    or is_junction()
                    or self._excluded_reason(relative + "/placeholder")
                ):
                    continue
                kept.append(directory)
            directories[:] = kept
            for filename in sorted(filenames, key=lambda item: (item.casefold(), item)):
                candidate = current_path / filename
                relative = candidate.relative_to(self.root).as_posix()
                is_junction = getattr(candidate, "is_junction", lambda: False)
                if candidate.is_symlink() or is_junction():
                    continue
                yield relative


@dataclass(frozen=True)
class IndexStats:
    scanned_files: int
    indexed_files: int
    indexed_symbols: int
    indexed_bytes: int
    skipped_by_reason: Mapping[str, int]
    truncated: bool


@dataclass(frozen=True)
class _IndexedFile:
    record: FileRecord
    text: str
    line_tokens: tuple[frozenset[str], ...]
    all_tokens: frozenset[str]


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(token.casefold() for token in _TOKEN_RE.findall(text))


class _SymbolVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.stack: list[str] = []
        self.symbols: list[SymbolRecord] = []

    def _visit_scope(self, node: ast.AST, kind: str) -> None:
        name = getattr(node, "name")
        qualified = ".".join((*self.stack, name))
        self.symbols.append(
            SymbolRecord(
                relative_path=self.relative_path,
                name=name,
                qualified_name=qualified,
                kind=kind,
                start_line=int(getattr(node, "lineno", 1)),
                end_line=int(getattr(node, "end_lineno", getattr(node, "lineno", 1))),
            )
        )
        self.stack.append(name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node, "class")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node, "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node, "async_function")


class RepositoryIndex:
    """Immutable in-memory index; search results are candidates, not read evidence."""

    def __init__(
        self,
        workspace: AuthorizedWorkspace,
        files: tuple[_IndexedFile, ...],
        symbols: tuple[SymbolRecord, ...],
        stats: IndexStats,
    ) -> None:
        self.workspace = workspace
        self.files = files
        self.symbols = symbols
        self.stats = stats
        self.index_id = str(uuid.uuid4())
        self._by_path = {item.record.relative_path: item for item in files}

    @property
    def file_records(self) -> tuple[FileRecord, ...]:
        return tuple(item.record for item in self.files)

    def search(
        self,
        query: str,
        *,
        path_filter: str | None = None,
        symbol_filter: str | None = None,
        limit: int = 10,
    ) -> tuple[SearchHit, ...]:
        if (
            not isinstance(limit, int)
            or limit < 1
            or limit > self.workspace.limits.max_search_results
        ):
            raise RepoError(
                RepoErrorCode.INVALID_LIMIT,
                "result limit is outside the configured range",
                details={"max_limit": self.workspace.limits.max_search_results},
            )
        query_terms = tuple(
            dict.fromkeys(_tokens(query.strip() if isinstance(query, str) else ""))
        )
        if not query_terms:
            raise RepoError(
                RepoErrorCode.INVALID_QUERY, "search query must contain searchable text"
            )
        path_needle = path_filter.replace("\\", "/").casefold() if path_filter else None
        symbol_needle = symbol_filter.casefold() if symbol_filter else None
        candidates: list[SearchHit] = []

        for indexed in self.files:
            relative = indexed.record.relative_path
            if path_needle and path_needle not in relative.casefold():
                continue
            file_symbols = tuple(
                symbol
                for symbol in self.symbols
                if symbol.relative_path == relative
                and (
                    symbol_needle is None
                    or symbol_needle in symbol.qualified_name.casefold()
                )
            )
            if symbol_needle and not file_symbols:
                continue

            path_tokens = frozenset(_tokens(relative))
            symbol_tokens = frozenset(
                token
                for symbol in file_symbols
                for token in _tokens(symbol.qualified_name)
            )
            matched = tuple(
                term
                for term in query_terms
                if term in indexed.all_tokens
                or term in path_tokens
                or term in symbol_tokens
            )
            if not matched or indexed.record.line_count == 0:
                continue
            score = sum(
                (12 if term in symbol_tokens else 0)
                + (6 if term in path_tokens else 0)
                + (1 if term in indexed.all_tokens else 0)
                for term in matched
            )
            line_numbers = tuple(
                line_no
                for line_no, tokens in enumerate(indexed.line_tokens, start=1)
                if any(term in tokens for term in matched)
            )
            matched_symbols = tuple(
                symbol.qualified_name
                for symbol in file_symbols
                if any(term in _tokens(symbol.qualified_name) for term in matched)
            )
            if not line_numbers and file_symbols:
                line_numbers = tuple(symbol.start_line for symbol in file_symbols)
            if not line_numbers:
                line_numbers = (1,)
            candidates.append(
                SearchHit(
                    relative_path=relative,
                    score=score,
                    matched_terms=matched,
                    matched_line_numbers=line_numbers,
                    matched_symbols=matched_symbols,
                    indexed_sha256=indexed.record.sha256,
                    line_count=indexed.record.line_count,
                )
            )

        candidates.sort(
            key=lambda hit: (
                -hit.score,
                hit.relative_path.casefold(),
                hit.relative_path,
            )
        )
        return tuple(candidates[:limit])


def build_index(workspace: AuthorizedWorkspace) -> RepositoryIndex:
    files: list[_IndexedFile] = []
    symbols: list[SymbolRecord] = []
    skipped: Counter[str] = Counter()
    scanned = 0
    total_bytes = 0
    truncated = False

    for relative in workspace.iter_files():
        if scanned >= workspace.limits.max_files:
            truncated = True
            break
        scanned += 1
        excluded = workspace._excluded_reason(relative)
        if excluded:
            skipped[excluded] += 1
            continue
        if not workspace._supported_text(relative):
            skipped["unsupported_type"] += 1
            continue
        try:
            path, canonical = workspace._resolve_existing(relative)
            size = path.stat().st_size
            if size > workspace.limits.max_file_bytes:
                skipped[RepoErrorCode.FILE_TOO_LARGE.value] += 1
                continue
            if total_bytes + size > workspace.limits.max_total_index_bytes:
                truncated = True
                break
            raw, canonical = workspace._read_stable_bytes(relative)
            if total_bytes + len(raw) > workspace.limits.max_total_index_bytes:
                truncated = True
                break
            if b"\x00" in raw:
                skipped["binary_content"] += 1
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                skipped["invalid_utf8"] += 1
                continue
        except RepoError as exc:
            skipped[exc.code.value] += 1
            continue
        except OSError:
            skipped[RepoErrorCode.IO_ERROR.value] += 1
            continue

        total_bytes += len(raw)
        line_text = text.splitlines()
        record = FileRecord(
            relative_path=canonical,
            size_bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            line_count=len(line_text),
            language=Path(canonical).suffix.lower().lstrip(".") or "text",
        )
        line_tokens = tuple(frozenset(_tokens(line)) for line in line_text)
        all_tokens = frozenset(_tokens(text))
        files.append(_IndexedFile(record, text, line_tokens, all_tokens))

        if Path(canonical).suffix.casefold() in {".py", ".pyi"}:
            try:
                tree = ast.parse(text, filename=canonical)
            except (SyntaxError, ValueError):
                skipped["python_syntax_error"] += 1
            else:
                visitor = _SymbolVisitor(canonical)
                visitor.visit(tree)
                symbols.extend(visitor.symbols)

    stats = IndexStats(
        scanned_files=scanned,
        indexed_files=len(files),
        indexed_symbols=len(symbols),
        indexed_bytes=total_bytes,
        skipped_by_reason=dict(sorted(skipped.items())),
        truncated=truncated,
    )
    return RepositoryIndex(workspace, tuple(files), tuple(symbols), stats)
