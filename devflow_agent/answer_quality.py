"""Deterministic, deliberately limited answer-quality checks.

This module evaluates only bounded arithmetic expressions and citation
provenance. It does not prove arbitrary natural-language claims.
"""

from __future__ import annotations

import ast
import hashlib
import math
import re
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping


_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_RELATION = r"(?:==|=|\bequals?\b|\breturns?\b|\bis\b)"
_SUM_CLAIM_RE = re.compile(
    rf"(?P<expression>\bsum\s*\([^)\n]{{0,256}}\))\s*\x60?\s*{_RELATION}\s*\x60?(?P<claimed>{_NUMBER})(?![\w.])",
    re.IGNORECASE,
)
_CALL_CLAIM_RE = re.compile(
    rf"(?P<expression>[A-Za-z_]\w*\s*\([^=\n]{{1,256}}?\)(?:\.[A-Za-z_]\w*\s*\([^=\n]{{0,128}}?\))?)\s*\x60?\s*{_RELATION}\s*\x60?(?P<claimed>{_NUMBER})(?![\w.])",
    re.IGNORECASE,
)
_ARITHMETIC_CLAIM_RE = re.compile(
    rf"(?<![A-Za-z0-9_.])\x60?(?P<expression>[+\-]?(?:\d|\()[\d.eE+\-*/%() \t]{{0,159}}?)\x60?\s*{_RELATION}\s*\x60?(?P<claimed>{_NUMBER})(?![\w.])",
    re.IGNORECASE,
)
_STRUCTURED_EXPRESSION_RE = re.compile(
    r"(?im)\bexpression\b\s*:\s*\*{0,2}\s*\x60?(?P<expression>"
    r"sum\s*\(\s*\[[^\n\x60]{0,256}\]\s*\)"
    r"|[+\-]?(?:\d|\()[\d.eE+\-*/%() \t]{0,159})\x60?"
)
_STRUCTURED_RESULT_RE = re.compile(
    rf"(?im)\bresult\b\s*:\s*\*{{0,2}}\s*\x60?\s*(?P<claimed>{_NUMBER})\s*\x60?"
)
_LITERAL_EXPRESSION_RE = re.compile(
    r"\bsum\s*\(\s*\[[^\n`]{0,256}\]\s*\)",
    re.IGNORECASE,
)
_PYTEST_NODEID_RE = re.compile(r"(?m)^FAILED\s+(?P<nodeid>[^\s]+)")
_PYTEST_TEST_FRAME_RE = re.compile(
    r"(?m)^(?P<path>[^\r\n]+?\.py):(?P<line>\d+):(?: in (?P<function>[A-Za-z_]\w*)| (?P<exception>[A-Za-z_]\w*))\s*$"
)
_PYTEST_ASSERTION_VALUES_RE = re.compile(
    rf"(?m)^.*?(?:AssertionError:\s*)?assert\s*(?P<actual>{_NUMBER})\s*==\s*(?P<expected>{_NUMBER})\s*$"
)
_OBSERVED_RESULT_RE = re.compile(
    rf"(?i)^Observed(?: pytest)? result\s*:\s*actual\s*=\s*(?P<actual>{_NUMBER})\s*[;,]\s*expected\s*=\s*(?P<expected>{_NUMBER})$"
)
_DIAGNOSIS_CLASS_RE = re.compile(
    r"(?i)^Diagnosis class\s*:\s*`?\*?(?P<class>VERIFIED_ASSERTION_MISMATCH|UNVERIFIABLE)\b"
)
_MAX_EXPRESSION_CHARS = 160
_MAX_AST_NODES = 64
_MAX_AST_DEPTH = 16
_MAX_ABS_INPUT = 1_000_000_000_000
_MAX_ABS_RESULT = 1e100
_FLOAT_REL_TOLERANCE = 1e-9
_FLOAT_ABS_TOLERANCE = 1e-9


class _UnverifiableExpression(ValueError):
    pass


def _check_number(value: int | float) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _UnverifiableExpression("non_numeric_value")
    if isinstance(value, float) and not math.isfinite(value):
        raise _UnverifiableExpression("non_finite_value")
    if abs(value) > _MAX_ABS_RESULT:
        raise _UnverifiableExpression("numeric_magnitude_exceeded")
    return value


def _literal_number(node: ast.AST) -> int | float:
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        value = _check_number(node.value)
        if abs(value) > _MAX_ABS_INPUT:
            raise _UnverifiableExpression("numeric_input_limit_exceeded")
        return value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _literal_number(node.operand)
        return _check_number(value if isinstance(node.op, ast.UAdd) else -value)
    raise _UnverifiableExpression("sum_requires_literal_numbers")


def _evaluate_node(node: ast.AST, depth: int = 0) -> int | float:
    if depth > _MAX_AST_DEPTH:
        raise _UnverifiableExpression("expression_depth_exceeded")
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        value = _check_number(node.value)
        if abs(value) > _MAX_ABS_INPUT:
            raise _UnverifiableExpression("numeric_input_limit_exceeded")
        return value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _evaluate_node(node.operand, depth + 1)
        return _check_number(value if isinstance(node.op, ast.UAdd) else -value)
    if isinstance(node, ast.Call):
        if (
            not isinstance(node.func, ast.Name)
            or node.func.id != "sum"
            or len(node.args) != 1
            or node.keywords
            or not isinstance(node.args[0], ast.List)
            or len(node.args[0].elts) > 64
        ):
            raise _UnverifiableExpression("unsupported_call")
        total: int | float = 0
        for item in node.args[0].elts:
            total = _check_number(total + _literal_number(item))
        return total
    if not isinstance(node, ast.BinOp):
        raise _UnverifiableExpression("unsupported_expression_node")
    left = _evaluate_node(node.left, depth + 1)
    right = _evaluate_node(node.right, depth + 1)
    try:
        if isinstance(node.op, ast.Add):
            result = left + right
        elif isinstance(node.op, ast.Sub):
            result = left - right
        elif isinstance(node.op, ast.Mult):
            result = left * right
        elif isinstance(node.op, ast.Div):
            if right == 0:
                raise _UnverifiableExpression("division_by_zero")
            result = left / right
        elif isinstance(node.op, ast.FloorDiv):
            if right == 0:
                raise _UnverifiableExpression("division_by_zero")
            result = left // right
        elif isinstance(node.op, ast.Mod):
            if right == 0:
                raise _UnverifiableExpression("division_by_zero")
            result = left % right
        elif isinstance(node.op, ast.Pow):
            if abs(right) > 12:
                raise _UnverifiableExpression("exponent_limit_exceeded")
            result = left**right
        else:
            raise _UnverifiableExpression("unsupported_operator")
    except (ArithmeticError, OverflowError) as exc:
        raise _UnverifiableExpression("arithmetic_error") from exc
    return _check_number(result)


def evaluate_numeric_expression(expression: str) -> int | float:
    """Evaluate a small arithmetic grammar without eval/exec or name lookup."""
    if not isinstance(expression, str) or not expression.strip():
        raise _UnverifiableExpression("empty_expression")
    if len(expression) > _MAX_EXPRESSION_CHARS:
        raise _UnverifiableExpression("expression_length_exceeded")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, ValueError, MemoryError) as exc:
        raise _UnverifiableExpression("invalid_expression_syntax") from exc
    if len(list(ast.walk(tree))) > _MAX_AST_NODES:
        raise _UnverifiableExpression("expression_node_limit_exceeded")
    return _evaluate_node(tree.body)


def _numeric_close(actual: int | float, claimed: int | float) -> bool:
    if type(actual) is int and type(claimed) is int:
        return actual == claimed
    return math.isclose(
        float(actual),
        float(claimed),
        rel_tol=_FLOAT_REL_TOLERANCE,
        abs_tol=_FLOAT_ABS_TOLERANCE,
    )


def extract_expected_numeric_claims(text: str) -> list[tuple[str, int | float]]:
    """Extract only bounded literal-list expressions explicitly requested by a task."""
    if not isinstance(text, str):
        return []
    expected: list[tuple[str, int | float]] = []
    for match in _LITERAL_EXPRESSION_RE.finditer(text):
        expression = match.group(0).strip()
        try:
            result = evaluate_numeric_expression(expression)
        except ValueError:
            continue
        item = (expression, result)
        if item not in expected:
            expected.append(item)
    return expected


def _trusted_read_documents(
    trusted_reads: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return only structurally valid trusted Python read snapshots.

    The caller remains responsible for current-file freshness and citation
    binding.  This helper verifies the evidence shape and fragment digest so
    static analysis never consumes arbitrary model-provided source text.
    """
    documents: list[dict[str, Any]] = []
    for evidence in trusted_reads:
        if not isinstance(evidence, Mapping):
            continue
        path = evidence.get("relative_path")
        content = evidence.get("content")
        fragment_sha256 = evidence.get("fragment_sha256")
        full_sha256 = evidence.get("full_file_sha256")
        start_line = evidence.get("start_line")
        end_line = evidence.get("end_line")
        line_count = evidence.get("line_count")
        pure_path = PurePosixPath(path) if isinstance(path, str) else None
        if (
            evidence.get("result_kind") != "trusted_file_read"
            or not isinstance(path, str)
            or "\\" in path
            or pure_path is None
            or pure_path.is_absolute()
            or ".." in pure_path.parts
            or not path.lower().endswith((".py", ".pyi"))
            or not isinstance(content, str)
            or not isinstance(start_line, int)
            or isinstance(start_line, bool)
            or not isinstance(end_line, int)
            or isinstance(end_line, bool)
            or not isinstance(line_count, int)
            or isinstance(line_count, bool)
            or start_line != 1
            or end_line != line_count
            or len(content.splitlines()) != line_count
            or not isinstance(fragment_sha256, str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", fragment_sha256)
            or hashlib.sha256(content.encode("utf-8")).hexdigest()
            != fragment_sha256.lower()
            or not isinstance(full_sha256, str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", full_sha256)
        ):
            continue
        documents.append(
            {
                "relative_path": path,
                "content": content,
                "start_line": start_line,
                "end_line": end_line,
                "line_count": line_count,
                "full_file_sha256": full_sha256.lower(),
            }
        )
    return documents


def _safe_static_value(
    node: ast.AST,
    environment: Mapping[str, Any],
    depth: int = 0,
) -> Any:
    """Evaluate a tiny AST expression with a closed value environment."""
    if depth > _MAX_AST_DEPTH:
        raise _UnverifiableExpression("static_expression_depth_exceeded")
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return _check_number(node.value)
    if isinstance(node, ast.Name):
        if node.id not in environment:
            raise _UnverifiableExpression("external_name")
        return environment[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _safe_static_value(node.operand, environment, depth + 1)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise _UnverifiableExpression("unary_non_numeric_value")
        return _check_number(value if isinstance(node.op, ast.UAdd) else -value)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_safe_static_value(item, environment, depth + 1) for item in node.elts]
    if isinstance(node, ast.Call):
        if (
            not isinstance(node.func, ast.Name)
            or node.func.id != "sum"
            or len(node.args) != 1
            or node.keywords
        ):
            raise _UnverifiableExpression("external_or_dynamic_call")
        values = _safe_static_value(node.args[0], environment, depth + 1)
        if not isinstance(values, list) or len(values) > 64:
            raise _UnverifiableExpression("sum_requires_static_sequence")
        total: int | float = 0
        for value in values:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise _UnverifiableExpression("sum_requires_numeric_sequence")
            total = _check_number(total + value)
        return total
    if isinstance(node, ast.BinOp):
        left = _safe_static_value(node.left, environment, depth + 1)
        right = _safe_static_value(node.right, environment, depth + 1)
        if not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in (left, right)
        ):
            raise _UnverifiableExpression("binary_non_numeric_value")
        try:
            if isinstance(node.op, ast.Add):
                result = left + right
            elif isinstance(node.op, ast.Sub):
                result = left - right
            elif isinstance(node.op, ast.Mult):
                result = left * right
            elif isinstance(node.op, ast.Div):
                if right == 0:
                    raise _UnverifiableExpression("division_by_zero")
                result = left / right
            else:
                raise _UnverifiableExpression("unsupported_static_operator")
        except (ArithmeticError, OverflowError) as exc:
            raise _UnverifiableExpression("static_arithmetic_error") from exc
        return _check_number(result)
    raise _UnverifiableExpression("unsupported_static_expression")


def _evaluate_static_project_call(
    expression: str,
    trusted_reads: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Evaluate one deliberately tiny, source-backed project function call."""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, ValueError, MemoryError):
        return {"applicability": "UNSUPPORTED", "reason": "invalid_call_syntax"}
    call = tree.body
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
        return {"applicability": "UNSUPPORTED", "reason": "not_a_project_function_call"}
    if call.keywords or len(call.args) == 0:
        return {"applicability": "UNSUPPORTED", "reason": "unsupported_call_signature"}
    try:
        argument_values = [_safe_static_value(argument, {}) for argument in call.args]
    except _UnverifiableExpression as exc:
        return {"applicability": "UNSUPPORTED", "reason": str(exc)}

    candidates: list[tuple[dict[str, Any], ast.FunctionDef]] = []
    for document in _trusted_read_documents(trusted_reads):
        try:
            module = ast.parse(document["content"], filename=document["relative_path"])
        except (SyntaxError, ValueError, MemoryError):
            continue
        for node in module.body:
            if isinstance(node, ast.FunctionDef) and node.name == call.func.id:
                candidates.append((document, node))
    if not candidates:
        return {
            "applicability": "UNSUPPORTED",
            "reason": "trusted_function_source_missing",
        }
    if len(candidates) != 1:
        return {"applicability": "UNSUPPORTED", "reason": "ambiguous_function_source"}

    document, function = candidates[0]
    arguments = function.args
    if (
        function.decorator_list
        or function.args.vararg is not None
        or function.args.kwarg is not None
        or function.args.kwonlyargs
        or function.args.defaults
        or function.args.kw_defaults
        or len(arguments.posonlyargs) + len(arguments.args) != len(argument_values)
    ):
        return {
            "applicability": "UNSUPPORTED",
            "reason": "unsupported_function_signature",
        }
    body = list(function.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
        return {"applicability": "UNSUPPORTED", "reason": "unsupported_control_flow"}
    parameter_names = [item.arg for item in arguments.posonlyargs + arguments.args]
    environment = dict(zip(parameter_names, argument_values))
    try:
        actual = _safe_static_value(body[0].value, environment)
    except _UnverifiableExpression as exc:
        return {"applicability": "UNSUPPORTED", "reason": str(exc)}
    if not isinstance(actual, (int, float)) or isinstance(actual, bool):
        return {"applicability": "UNSUPPORTED", "reason": "non_numeric_return"}
    start_line = document.get("start_line")
    source_line = (
        body[0].lineno + start_line - 1
        if isinstance(start_line, int)
        else body[0].lineno
    )
    return {
        "applicability": "STATICALLY_VERIFIABLE",
        "actual_value": actual,
        "source_path": document["relative_path"],
        "source_line": source_line,
        "source_sha256": document["full_file_sha256"],
    }


def _parse_numeric_literal(value: str) -> int | float:
    parsed: int | float = float(value)
    if re.fullmatch(r"[+-]?\d+", value):
        parsed = int(value)
    return _check_number(parsed)


def _source_module_name(relative_path: str) -> str:
    return PurePosixPath(relative_path).with_suffix("").as_posix().replace("/", ".")


def _find_trusted_test_expectation(
    expression: str,
    claimed_value: int | float,
    trusted_reads: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Verify that a numeric expectation appears in a trusted test AST.

    The assertion's expected value is never treated as a runtime result. When
    the implementation is also statically simple, its result is recorded as
    a separate fact so the two values cannot be conflated.
    """
    try:
        expression_tree = ast.parse(expression, mode="eval").body
    except (SyntaxError, ValueError, MemoryError):
        return {"verification": "UNVERIFIABLE", "reason": "invalid_test_call_syntax"}
    if not isinstance(expression_tree, ast.Call) or not isinstance(
        expression_tree.func, ast.Name
    ):
        return {"verification": "UNVERIFIABLE", "reason": "unsupported_test_call"}

    documents = _trusted_read_documents(trusted_reads)
    python_paths = {item["relative_path"] for item in documents}
    matches: list[dict[str, Any]] = []
    call_dump = ast.dump(expression_tree, include_attributes=False)
    expected_function = expression_tree.func.id
    for document in documents:
        path = document["relative_path"]
        if not (path.startswith("tests/") or "/tests/" in path):
            continue
        try:
            module = ast.parse(document["content"], filename=path)
        except (SyntaxError, ValueError, MemoryError):
            continue
        imported_modules = {
            alias.name: node.module
            for node in module.body
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
            for alias in node.names
            if alias.asname is None and alias.name == expected_function
        }
        for node in ast.walk(module):
            if not isinstance(node, ast.Assert) or not isinstance(
                node.test, ast.Compare
            ):
                continue
            comparison = node.test
            if (
                len(comparison.ops) != 1
                or not isinstance(comparison.ops[0], ast.Eq)
                or len(comparison.comparators) != 1
                or not isinstance(comparison.left, ast.Call)
                or not isinstance(comparison.left.func, ast.Name)
                or comparison.left.func.id != expected_function
                or ast.dump(comparison.left, include_attributes=False) != call_dump
            ):
                continue
            try:
                test_expected = _safe_static_value(comparison.comparators[0], {})
            except _UnverifiableExpression:
                continue
            if (
                isinstance(test_expected, bool)
                or not isinstance(test_expected, (int, float))
                or not _numeric_close(test_expected, claimed_value)
            ):
                continue
            source_path = next(
                (
                    source
                    for source in python_paths
                    if source.endswith((".py", ".pyi"))
                    and imported_modules.get(expected_function)
                    == _source_module_name(source)
                ),
                None,
            )
            if source_path is None:
                continue
            call_text = ast.get_source_segment(document["content"], comparison.left)
            if not call_text:
                continue
            source_behavior = _evaluate_static_project_call(call_text, trusted_reads)
            matches.append(
                {
                    "test_path": path,
                    "test_line": node.lineno,
                    "test_sha256": document["full_file_sha256"],
                    "test_expected_value": test_expected,
                    "source_path": source_behavior.get("source_path", source_path),
                    "source_line": source_behavior.get("source_line"),
                    "source_sha256": source_behavior.get("source_sha256"),
                    "source_static_applicability": source_behavior.get(
                        "applicability", "UNSUPPORTED"
                    ),
                    "source_static_value": source_behavior.get("actual_value"),
                    "matches_source_static_value": (
                        _numeric_close(
                            source_behavior.get("actual_value"), test_expected
                        )
                        if source_behavior.get("actual_value") is not None
                        else None
                    ),
                }
            )
    if len(matches) != 1:
        return {
            "verification": "UNVERIFIABLE",
            "classification": "UNVERIFIABLE",
            "reason": "trusted_test_expectation_missing_or_ambiguous",
        }
    result = matches[0]
    result.update(
        {
            "verification": "SOURCE_FACT_VERIFIED",
            "classification": "VERIFIED_SOURCE_OR_TEST",
            "runtime_observation": "NOT_VERIFIED_BY_TEST_SOURCE_ALONE",
        }
    )
    return result


def _complete_trusted_text_documents(
    trusted_reads: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for evidence in trusted_reads:
        if not isinstance(evidence, Mapping):
            continue
        path = evidence.get("relative_path")
        content = evidence.get("content")
        start_line = evidence.get("start_line")
        end_line = evidence.get("end_line")
        line_count = evidence.get("line_count")
        fragment_hash = evidence.get("fragment_sha256")
        full_hash = evidence.get("full_file_sha256")
        pure_path = PurePosixPath(path) if isinstance(path, str) else None
        if (
            evidence.get("result_kind") != "trusted_file_read"
            or not isinstance(path, str)
            or "\\" in path
            or pure_path is None
            or pure_path.is_absolute()
            or ".." in pure_path.parts
            or not isinstance(content, str)
            or not isinstance(start_line, int)
            or isinstance(start_line, bool)
            or not isinstance(end_line, int)
            or isinstance(end_line, bool)
            or not isinstance(line_count, int)
            or isinstance(line_count, bool)
            or start_line != 1
            or end_line != line_count
            or len(content.splitlines()) != line_count
            or not isinstance(fragment_hash, str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", fragment_hash)
            or hashlib.sha256(content.encode("utf-8")).hexdigest()
            != fragment_hash.lower()
            or not isinstance(full_hash, str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", full_hash)
        ):
            continue
        documents.append(
            {
                "relative_path": path,
                "content": content,
                "line_count": line_count,
                "full_file_sha256": full_hash.lower(),
            }
        )
    return documents


def _line_covering_citation(
    citation_check: Mapping[str, Any], path: str, line: int
) -> bool:
    return any(
        item.get("relative_path") == path
        and isinstance(item.get("start_line"), int)
        and isinstance(item.get("end_line"), int)
        and item["start_line"] <= line <= item["end_line"]
        for item in citation_check.get("citations", [])
        if isinstance(item, Mapping)
    )


def evaluate_pytest_diagnostic_evidence(
    question: str,
    answer: str,
    trusted_reads: Iterable[Mapping[str, Any]],
    citation_check: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify one bounded numeric pytest assertion against its test and source.

    This verifies an observed assertion mismatch, not the intended product
    contract or every sentence in a natural-language root-cause explanation.
    It accepts only full trusted reads, one numeric pytest assertion, one
    directly imported simple project function, and a structured answer pair.
    """
    if not isinstance(question, str) or not re.search(
        r"(?i)pytest|test failure|captured failure", question
    ):
        return {"status": "NOT_APPLICABLE", "claims": [], "errors": []}
    if not citation_check.get("valid"):
        return {
            "status": "UNVERIFIABLE",
            "claims": [],
            "errors": ["diagnostic_citations_not_valid"],
        }

    trusted_read_list = list(trusted_reads)
    documents = _complete_trusted_text_documents(trusted_read_list)
    python_reads = [
        item
        for item in trusted_read_list
        if isinstance(item, Mapping)
        and isinstance(item.get("relative_path"), str)
        and item["relative_path"].lower().endswith((".py", ".pyi"))
    ]
    python_documents = _trusted_read_documents(python_reads)
    expectation_facts: list[dict[str, Any]] = []
    for match in _CALL_CLAIM_RE.finditer(answer or ""):
        if not _looks_like_source_expectation(answer, match):
            continue
        try:
            claimed = _parse_numeric_literal(match.group("claimed"))
        except (ValueError, OverflowError, _UnverifiableExpression):
            continue
        fact = _find_trusted_test_expectation(
            match.group("expression").strip().strip("`").strip(),
            claimed,
            python_reads,
        )
        expectation_facts.append(fact)

    log_documents = [
        item
        for item in documents
        if item["relative_path"].lower().endswith((".txt", ".log"))
        and re.search(r"(?im)^FAILED\s+", item["content"])
    ]
    if len(log_documents) != 1:
        return {
            "status": "UNVERIFIABLE",
            "claims": expectation_facts
            + [
                {
                    "kind": "observed_test_result",
                    "classification": "UNVERIFIABLE",
                    "reason": "one_complete_trusted_pytest_log_required",
                },
                {
                    "kind": "root_cause_interpretation",
                    "classification": "MODEL_INFERENCE_UNVERIFIED",
                    "reason": "observed_pytest_result_not_bound_to_test_and_source",
                },
            ],
            "errors": ["trusted_pytest_failure_log_missing_or_ambiguous"],
        }

    log = log_documents[0]
    nodeids = list(_PYTEST_NODEID_RE.finditer(log["content"]))
    assertion_values = list(_PYTEST_ASSERTION_VALUES_RE.finditer(log["content"]))
    assertion_pairs = {
        (item.group("actual"), item.group("expected")) for item in assertion_values
    }
    if len(nodeids) != 1 or len(assertion_pairs) != 1 or not assertion_values:
        return {
            "status": "UNVERIFIABLE",
            "claims": expectation_facts
            + [
                {
                    "kind": "observed_test_result",
                    "classification": "UNVERIFIABLE",
                    "reason": "pytest_log_must_have_one_numeric_assertion_failure",
                }
            ],
            "errors": ["pytest_assertion_result_missing_or_ambiguous"],
        }
    nodeid = nodeids[0].group("nodeid")
    test_path, _, test_name = nodeid.partition("::")
    test_function_name = test_name.rsplit("[", 1)[0]
    matching_test_frames = [
        frame
        for frame in _PYTEST_TEST_FRAME_RE.finditer(log["content"])
        if frame.group("path").replace("\\", "/") == test_path
        and frame.group("exception") in (None, "AssertionError")
        and frame.group("function") in (None, test_function_name)
    ]
    if len(matching_test_frames) != 1:
        return {
            "status": "UNVERIFIABLE",
            "claims": expectation_facts,
            "errors": ["pytest_test_frame_missing_or_ambiguous"],
        }
    log_test_line = int(matching_test_frames[0].group("line"))
    selected_assertion = next(
        (item for item in assertion_values if "AssertionError:" in item.group(0)),
        assertion_values[0],
    )
    try:
        observed_actual = _parse_numeric_literal(selected_assertion.group("actual"))
        observed_expected = _parse_numeric_literal(selected_assertion.group("expected"))
    except (ValueError, OverflowError, _UnverifiableExpression):
        return {
            "status": "UNVERIFIABLE",
            "claims": expectation_facts,
            "errors": ["pytest_assertion_values_not_supported"],
        }
    log_line = log["content"][: selected_assertion.start()].count("\n") + 1

    test_documents = [
        item for item in python_documents if item["relative_path"] == test_path
    ]
    if len(test_documents) != 1:
        return {
            "status": "UNVERIFIABLE",
            "claims": expectation_facts,
            "errors": ["failed_test_source_not_trusted_or_ambiguous"],
        }
    test_document = test_documents[0]
    try:
        test_module = ast.parse(test_document["content"], filename=test_path)
    except (SyntaxError, ValueError, MemoryError):
        return {
            "status": "UNVERIFIABLE",
            "claims": expectation_facts,
            "errors": ["failed_test_source_parse_error"],
        }
    matching_assertions: list[dict[str, Any]] = []
    for function in test_module.body:
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if function.name != test_function_name:
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.Assert) or not isinstance(
                node.test, ast.Compare
            ):
                continue
            if node.lineno != log_test_line:
                continue
            comparison = node.test
            if (
                len(comparison.ops) != 1
                or not isinstance(comparison.ops[0], ast.Eq)
                or len(comparison.comparators) != 1
                or not isinstance(comparison.left, ast.Call)
                or not isinstance(comparison.left.func, ast.Name)
            ):
                continue
            function_name = comparison.left.func.id
            expected_module = None
            for import_node in test_module.body:
                if not isinstance(import_node, ast.ImportFrom) or import_node.level:
                    continue
                if any(
                    alias.name == function_name and alias.asname is None
                    for alias in import_node.names
                ):
                    expected_module = import_node.module
                    break
            if expected_module is None:
                continue
            expected_source_path = expected_module.replace(".", "/") + ".py"
            if expected_source_path not in {
                item["relative_path"] for item in python_documents
            }:
                continue
            call_expression = ast.get_source_segment(
                test_document["content"], comparison.left
            )
            if not call_expression:
                continue
            try:
                expected_value = _safe_static_value(comparison.comparators[0], {})
            except _UnverifiableExpression:
                continue
            if isinstance(expected_value, bool) or not isinstance(
                expected_value, (int, float)
            ):
                continue
            source_result = _evaluate_static_project_call(call_expression, python_reads)
            matching_assertions.append(
                {
                    "test_path": test_path,
                    "test_line": node.lineno,
                    "test_sha256": test_document["full_file_sha256"],
                    "test_expected_value": expected_value,
                    "source_path": source_result.get("source_path"),
                    "source_line": source_result.get("source_line"),
                    "source_sha256": source_result.get("source_sha256"),
                    "source_static_applicability": source_result.get(
                        "applicability", "UNSUPPORTED"
                    ),
                    "source_static_value": source_result.get("actual_value"),
                }
            )
    if len(matching_assertions) != 1:
        return {
            "status": "UNVERIFIABLE",
            "claims": expectation_facts,
            "errors": ["one_direct_test_assertion_and_source_function_required"],
        }
    fact = matching_assertions[0]
    if (
        fact["source_static_applicability"] != "STATICALLY_VERIFIABLE"
        or fact["source_static_value"] is None
    ):
        return {
            "status": "UNVERIFIABLE",
            "claims": expectation_facts
            + [
                {
                    "kind": "static_source_behavior",
                    "classification": "UNVERIFIABLE",
                    "reason": "source_function_outside_safe_static_subset",
                }
            ],
            "errors": ["source_function_not_statically_verifiable"],
        }

    evidence_consistent = (
        _numeric_close(observed_actual, fact["source_static_value"])
        and _numeric_close(observed_expected, fact["test_expected_value"])
        and not _numeric_close(observed_actual, observed_expected)
    )
    answer_result = None
    for line in (answer or "").splitlines():
        normalized = line.strip().replace("**", "").replace("`", "")
        match = _OBSERVED_RESULT_RE.fullmatch(normalized)
        if match:
            try:
                answer_result = (
                    _parse_numeric_literal(match.group("actual")),
                    _parse_numeric_literal(match.group("expected")),
                )
            except (ValueError, OverflowError, _UnverifiableExpression):
                answer_result = None
            break
    diagnosis_class = None
    for line in (answer or "").splitlines():
        match = _DIAGNOSIS_CLASS_RE.match(
            line.strip().replace("**", "").replace("`", "")
        )
        if match:
            diagnosis_class = match.group("class").upper()
            break

    cited = all(
        _line_covering_citation(citation_check, path, line)
        for path, line in (
            (log["relative_path"], log_line),
            (log["relative_path"], log_test_line),
            (fact["test_path"], fact["test_line"]),
            (fact["source_path"], fact["source_line"]),
        )
        if isinstance(line, int)
    ) and isinstance(fact.get("source_line"), int)
    claims = expectation_facts + [
        {
            "kind": "observed_pytest_assertion",
            "classification": "VERIFIED_SOURCE_OR_TEST"
            if evidence_consistent
            else "UNVERIFIABLE",
            "actual_value": observed_actual,
            "expected_value": observed_expected,
            "log_path": log["relative_path"],
            "log_line": log_line,
            "log_sha256": log["full_file_sha256"],
        },
        {
            "kind": "test_expectation",
            "classification": "VERIFIED_SOURCE_OR_TEST",
            "expected_value": fact["test_expected_value"],
            "path": fact["test_path"],
            "line": fact["test_line"],
            "sha256": fact["test_sha256"],
        },
        {
            "kind": "static_source_behavior",
            "classification": "VERIFIED_SOURCE_OR_TEST",
            "actual_value": fact["source_static_value"],
            "path": fact["source_path"],
            "line": fact["source_line"],
            "sha256": fact["source_sha256"],
            "execution_claimed": False,
        },
        {
            "kind": "root_cause_interpretation",
            "classification": "VERIFIED_SOURCE_OR_TEST"
            if diagnosis_class == "VERIFIED_ASSERTION_MISMATCH" and evidence_consistent
            else "MODEL_INFERENCE_UNVERIFIED",
            "diagnosis_class": diagnosis_class,
            "intended_contract": "NOT_PROVEN_BY_THIS_CHECK",
        },
    ]
    if not evidence_consistent:
        return {
            "status": "UNVERIFIABLE",
            "claims": claims,
            "errors": ["pytest_test_and_static_source_evidence_conflict"],
            "semantic_entailment": "NOT_AUTOMATICALLY_VERIFIED",
        }
    if answer_result is None:
        return {
            "status": "UNVERIFIABLE",
            "claims": claims,
            "errors": ["answer_missing_structured_observed_result"],
            "semantic_entailment": "NOT_AUTOMATICALLY_VERIFIED",
        }
    if not _numeric_close(answer_result[0], observed_actual) or not _numeric_close(
        answer_result[1], observed_expected
    ):
        return {
            "status": "DIAGNOSTIC_MISMATCH",
            "claims": claims,
            "errors": ["answer_observed_result_does_not_match_pytest_log"],
            "semantic_entailment": "NOT_AUTOMATICALLY_VERIFIED",
        }
    if diagnosis_class != "VERIFIED_ASSERTION_MISMATCH":
        return {
            "status": "DIAGNOSTIC_MISMATCH"
            if diagnosis_class is not None
            else "UNVERIFIABLE",
            "claims": claims,
            "errors": ["answer_diagnosis_class_missing_or_not_evidence_bound"],
            "semantic_entailment": "NOT_AUTOMATICALLY_VERIFIED",
        }
    if not cited:
        return {
            "status": "UNVERIFIABLE",
            "claims": claims,
            "errors": ["diagnostic_evidence_lines_not_all_cited"],
            "semantic_entailment": "NOT_AUTOMATICALLY_VERIFIED",
        }
    return {
        "status": "PASS",
        "claims": claims,
        "errors": [],
        "verified_conclusion": "ASSERTION_MISMATCH_ONLY",
        "intended_contract": "NOT_PROVEN_BY_THIS_CHECK",
        "semantic_entailment": "NOT_AUTOMATICALLY_VERIFIED",
    }


def _looks_like_source_expectation(answer: str, match: re.Match[str]) -> bool:
    """Avoid treating a quoted test expectation as a runtime behavior claim."""
    line_start = answer.rfind("\n", 0, match.start()) + 1
    line_end = answer.find("\n", match.end())
    if line_end < 0:
        line_end = len(answer)
    context = answer[line_start:line_end].lower()
    return any(
        marker in context for marker in ("expects", "assert", "test expects", "asserts")
    )


def _evaluate_numeric_claim_expression(
    expression: str,
    claimed_text: str,
    trusted_reads: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    try:
        claimed = _check_number(float(claimed_text))
        if re.fullmatch(r"[+-]?\d+", claimed_text):
            claimed = int(claimed_text)
    except (ValueError, OverflowError):
        return {
            "expression": expression,
            "claimed_value": claimed_text,
            "actual_value": None,
            "matches": None,
            "applicability": "UNSUPPORTED",
            "verification": "UNVERIFIABLE",
            "reason": "invalid_claimed_number",
        }
    try:
        actual = evaluate_numeric_expression(expression)
        applicability = "SUPPORTED"
        source_info: dict[str, Any] = {}
    except _UnverifiableExpression:
        source_info = _evaluate_static_project_call(expression, trusted_reads)
        applicability = source_info.get("applicability", "UNSUPPORTED")
        if applicability != "STATICALLY_VERIFIABLE":
            return {
                "expression": expression,
                "claimed_value": claimed,
                "actual_value": None,
                "matches": None,
                "applicability": applicability,
                "verification": "UNVERIFIABLE",
                "reason": source_info.get("reason", "unsupported_expression"),
            }
        actual = source_info["actual_value"]
    matches = _numeric_close(actual, claimed)
    result = {
        "expression": expression,
        "claimed_value": claimed,
        "actual_value": actual,
        "matches": matches,
        "applicability": applicability,
        "verification": "PASS" if matches else "NUMERIC_MISMATCH",
    }
    result.update(
        {key: value for key, value in source_info.items() if key != "applicability"}
    )
    return result


def evaluate_arithmetic_claims(
    answer: str,
    *,
    required: bool = False,
    expected_claims: Iterable[tuple[str, int | float]] = (),
    trusted_reads: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Check explicit bounded arithmetic claims and required task expressions.

    A response without a required numeric claim is ``NOT_APPLICABLE``; it is
    never treated as proof that a computation was verified.  A required task
    with no extractable claim is ``UNVERIFIABLE``.
    """
    if not isinstance(answer, str):
        return {"status": "UNVERIFIABLE", "claims": [], "errors": ["answer_not_text"]}
    expected = list(expected_claims)
    trusted_read_list = list(trusted_reads)
    candidates: list[tuple[int, int, re.Match[str]]] = []
    sum_matches = list(_SUM_CLAIM_RE.finditer(answer))
    candidates.extend((m.start(), m.end(), m) for m in sum_matches)
    for match in _CALL_CLAIM_RE.finditer(answer):
        if any(
            match.start() < end and match.end() > start for start, end, _ in candidates
        ):
            continue
        candidates.append((match.start(), match.end(), match))
    for match in _ARITHMETIC_CLAIM_RE.finditer(answer):
        if any(
            match.start() < end and match.end() > start for start, end, _ in candidates
        ):
            continue
        candidates.append((match.start(), match.end(), match))
    structured_expression = _STRUCTURED_EXPRESSION_RE.search(answer)
    structured_result = _STRUCTURED_RESULT_RE.search(answer)
    if structured_expression is not None and structured_result is not None:
        expression_start = structured_expression.start("expression")
        expression_end = structured_expression.end("expression")
        if not any(
            expression_start < end and expression_end > start
            for start, end, _ in candidates
        ):
            # The existing claim regexes require an adjacent relation.  The
            # structured Expression/Result form is a bounded equivalent.
            candidates.append(
                (
                    structured_expression.start(),
                    structured_result.end(),
                    (structured_expression, structured_result),
                )
            )
    candidates.sort(key=lambda item: item[0])

    claims: list[dict[str, Any]] = []
    source_expectations: list[dict[str, Any]] = []
    errors: list[str] = []
    for _, _, match in candidates:
        if isinstance(match, tuple):
            expression_match, result_match = match
            expression = (
                expression_match.group("expression").strip().strip("\x60").strip()
            )
            claimed_text = result_match.group("claimed")
        else:
            expression = match.group("expression").strip().strip("\x60").strip()
            claimed_text = match.group("claimed")
        if not isinstance(match, tuple) and _looks_like_source_expectation(
            answer, match
        ):
            try:
                expected_value = _parse_numeric_literal(claimed_text)
                expectation = _find_trusted_test_expectation(
                    expression, expected_value, trusted_read_list
                )
            except (ValueError, OverflowError, _UnverifiableExpression):
                expectation = {
                    "verification": "UNVERIFIABLE",
                    "classification": "UNVERIFIABLE",
                    "reason": "invalid_test_expectation_value",
                }
            expectation.update(
                {
                    "kind": "test_expectation",
                    "expression": expression,
                    "claimed_value": claimed_text,
                    "runtime_observation": "NOT_INFERRED_FROM_EXPECTATION",
                }
            )
            source_expectations.append(expectation)
            if expectation.get("verification") != "SOURCE_FACT_VERIFIED":
                errors.append("source_expectation_unverifiable")
            continue
        claim = _evaluate_numeric_claim_expression(
            expression, claimed_text, trusted_read_list
        )
        claims.append(claim)
        if claim.get("verification") == "NUMERIC_MISMATCH":
            errors.append("numeric_result_mismatch")
        elif claim.get("verification") == "UNVERIFIABLE":
            errors.append("numeric_claim_unverifiable")
    expected_errors: list[str] = []
    for expected_expression, expected_result in expected:
        normalized_expected = re.sub(r"\s+", "", expected_expression)
        matching_claim = next(
            (
                claim
                for claim in claims
                if re.sub(r"\s+", "", str(claim.get("expression", "")))
                == normalized_expected
            ),
            None,
        )
        if matching_claim is None:
            expected_errors.append("expected_numeric_claim_missing")
            continue
        if matching_claim.get("actual_value") is None or not _numeric_close(
            expected_result, matching_claim["actual_value"]
        ):
            expected_errors.append("expected_expression_unverifiable")
        elif not matching_claim.get("matches") or not _numeric_close(
            expected_result, matching_claim.get("claimed_value")
        ):
            errors.append("numeric_result_mismatch")
    errors.extend(expected_errors)
    if "numeric_result_mismatch" in errors:
        status = "NUMERIC_MISMATCH"
    elif errors:
        status = "UNVERIFIABLE"
    elif not claims:
        status = "UNVERIFIABLE" if required or expected else "NOT_APPLICABLE"
    else:
        status = "PASS"
    return {
        "status": status,
        "quality_state": "FAIL" if status == "NUMERIC_MISMATCH" else status,
        "claims_checked": len(claims),
        "claims": claims,
        "source_expectations": source_expectations,
        "errors": sorted(set(errors)),
        "scope": "SUPPORTED checks bounded arithmetic. Source/test expectations are classified separately and are never treated as observed runtime values. STATICALLY_VERIFIABLE source behavior requires one complete trusted Python function with a single side-effect-free return; branches, loops, dynamic calls and external state remain UNVERIFIABLE.",
    }


def evaluate_answer_quality(
    *,
    answer: str,
    run_status: str,
    terminal_reason: str,
    trace_issues: Iterable[str],
    read_evidence_count: int,
    citation_check: dict[str, Any],
    numeric_check: dict[str, Any],
    diagnostic_check: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Combine execution integrity, citation provenance and arithmetic checks."""
    diagnostic = diagnostic_check or {
        "status": "NOT_APPLICABLE",
        "claims": [],
        "errors": [],
    }
    issues = list(trace_issues)
    citation_errors = list(citation_check.get("errors") or [])
    answer_present = isinstance(answer, str) and bool(answer.strip())
    execution_ok = (
        run_status == "completed"
        and terminal_reason == "final_answer"
        and answer_present
        and not issues
    )
    if not execution_ok:
        status = "MODEL_OR_TOOL_FAILURE"
    elif read_evidence_count <= 0:
        status = "MISSING_EVIDENCE"
    elif any(
        error in citation_errors
        for error in (
            "invalid_citation_format",
            "citation_not_read",
            "citation_outside_read_range",
            "citation_stale_file_version",
            "citation_source_unavailable",
            "citation_evidence_mismatch",
            "invalid_read_evidence",
        )
    ):
        status = "INVALID_CITATION"
    elif not citation_check.get("valid") or not citation_check.get("citations"):
        status = "MISSING_EVIDENCE"
    elif numeric_check.get("status") == "NUMERIC_MISMATCH":
        status = "NUMERIC_MISMATCH"
    elif diagnostic.get("status") == "DIAGNOSTIC_MISMATCH":
        status = "DIAGNOSTIC_MISMATCH"
    elif (
        numeric_check.get("status") == "UNVERIFIABLE"
        or diagnostic.get("status") == "UNVERIFIABLE"
    ):
        status = "UNVERIFIABLE"
    elif numeric_check.get("status") not in {"PASS", "NOT_APPLICABLE"}:
        status = "UNVERIFIABLE"
    elif diagnostic.get("status") not in {"PASS", "NOT_APPLICABLE"}:
        status = "UNVERIFIABLE"
    else:
        status = "PASS"
    return {
        "status": status,
        "issues": sorted(
            set(issues + citation_errors + list(numeric_check.get("errors") or []))
        ),
        "components": {
            "execution": "PASS" if execution_ok else "MODEL_OR_TOOL_FAILURE",
            "citation": "PASS"
            if citation_check.get("valid")
            else (
                "INVALID_CITATION"
                if read_evidence_count > 0 and citation_check.get("citations")
                else "MISSING_EVIDENCE"
            ),
            "numeric": numeric_check.get("status", "UNVERIFIABLE"),
            "diagnostic": diagnostic.get("status", "NOT_APPLICABLE"),
            "semantic_entailment": "NOT_AUTOMATICALLY_VERIFIED",
        },
        "numeric_check": numeric_check,
        "diagnostic_check": diagnostic,
        "citation_check": citation_check,
        "scope_note": (
            "PASS means execution integrity, citation provenance, bounded numeric checks, and any applicable structured pytest assertion comparison passed. "
            "The diagnostic check verifies only an observed assertion mismatch against trusted test/source evidence; it does not prove intended behavior or every natural-language conclusion."
        ),
    }


def build_numeric_correction_prompt(numeric_check: dict[str, Any]) -> str:
    """Build one bounded correction request from verifier-produced arithmetic facts."""
    mismatches = [
        claim
        for claim in numeric_check.get("claims", [])
        if claim.get("verification") == "NUMERIC_MISMATCH"
    ]
    if not mismatches:
        raise ValueError("A correction prompt requires at least one verified mismatch")
    details = "\n".join(
        f"- Recompute {claim['expression']!r}; your answer stated {claim['claimed_value']}, "
        f"while the deterministic calculation is {claim['actual_value']}."
        for claim in mismatches[:8]
    )
    return (
        "A deterministic arithmetic check found a contradiction in your previous answer. "
        "Recompute the expressions below and provide one corrected complete answer. "
        "Keep citations tied to successful trusted reads already in this conversation. "
        "You may use only the existing read-only repository search and read tools if more source evidence is needed. "
        "Do not repeat the previous incorrect value or claim that the checker verified unrelated natural-language facts.\n"
        f"{details}"
    )
