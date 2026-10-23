"""Deterministic, deliberately limited answer-quality checks.

This module evaluates only bounded arithmetic expressions and citation
provenance. It does not prove arbitrary natural-language claims.
"""

from __future__ import annotations

import ast
import hashlib
import math
import re
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
        if (
            evidence.get("result_kind") != "trusted_file_read"
            or not isinstance(path, str)
            or not path.lower().endswith((".py", ".pyi"))
            or not isinstance(content, str)
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
                "start_line": evidence.get("start_line"),
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
        function.lineno + start_line - 1
        if isinstance(start_line, int)
        else function.lineno
    )
    return {
        "applicability": "STATICALLY_VERIFIABLE",
        "actual_value": actual,
        "source_path": document["relative_path"],
        "source_line": source_line,
        "source_sha256": document["full_file_sha256"],
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
            claims.append(
                {
                    "expression": expression,
                    "claimed_value": claimed_text,
                    "actual_value": None,
                    "matches": None,
                    "applicability": "UNSUPPORTED",
                    "verification": "UNVERIFIABLE",
                    "reason": "source_expectation_not_runtime_claim",
                }
            )
            errors.append("numeric_claim_unverifiable")
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
        "errors": sorted(set(errors)),
        "scope": "SUPPORTED uses bounded arithmetic; STATICALLY_VERIFIABLE uses one trusted Python function with a single side-effect-free return; branches, loops, dynamic calls and external state remain UNVERIFIABLE.",
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
) -> dict[str, Any]:
    """Combine execution integrity, citation provenance and arithmetic checks."""
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
    elif numeric_check.get("status") == "UNVERIFIABLE":
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
            "semantic_entailment": "NOT_AUTOMATICALLY_VERIFIED",
        },
        "numeric_check": numeric_check,
        "citation_check": citation_check,
        "scope_note": (
            "PASS means bounded arithmetic, execution integrity, and citation provenance checks passed. "
            "It does not prove that every natural-language conclusion is entailed by the cited source."
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
