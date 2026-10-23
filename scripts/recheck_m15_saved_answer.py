"""Offline-only re-evaluation of the preserved M1.5 real answer."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devflow_agent.answer_quality import (
    evaluate_answer_quality,
    evaluate_arithmetic_claims,
    extract_expected_numeric_claims,
)
from devflow_agent.real_readonly_agent import validate_answer_citations
from devflow_agent.repository import AuthorizedWorkspace


RUN_ID = "20261023T102601Z-m15-python-health"


def main() -> int:
    project = Path(__file__).resolve().parents[1]
    run_dir = project / "reports" / "m1_5_real_model_runs" / RUN_ID
    raw = json.loads((run_dir / "ask-run.json").read_text(encoding="utf-8"))
    answer = raw["final_answer"]
    read_result = next(
        item["result"]
        for item in raw["tool_trace"]
        if item.get("tool") == "repository_readonly_read"
    )
    # The original workspace UUID is intentionally not replaced in the raw
    # evidence.  A derived record binds the same bytes/hash to this temporary
    # mirror solely so the normal freshness checker can run offline.
    with tempfile.TemporaryDirectory(prefix="devflow-m15-recheck-") as temporary:
        root = Path(temporary)
        source = root / read_result["relative_path"]
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(read_result["content"].encode("utf-8"))
        workspace = AuthorizedWorkspace(root)
        derived = copy.deepcopy(read_result)
        derived["workspace_id"] = workspace.workspace_id
        citation = validate_answer_citations(answer, [derived], workspace=workspace)
        expected = extract_expected_numeric_claims(raw["question"])
        numeric = evaluate_arithmetic_claims(
            answer,
            required=bool(expected),
            expected_claims=expected,
        )
        quality = evaluate_answer_quality(
            answer=answer,
            run_status="completed",
            terminal_reason="final_answer",
            trace_issues=raw.get("trace_issues", []),
            read_evidence_count=1,
            citation_check=citation,
            numeric_check=numeric,
        )
        disk_hash = workspace.read_text(source.relative_to(root).as_posix()).full_file_sha256
    result = {
        "kind": "offline_recheck_of_preserved_real_model_answer",
        "run_id": RUN_ID,
        "original_ask_exit_code": raw.get("ask_exit_code"),
        "original_answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
        "citation": citation,
        "numeric": numeric,
        "quality": quality,
        "recorded_read_sha256": read_result["full_file_sha256"],
        "disk_sha256_in_temporary_mirror": disk_hash,
        "raw_evidence_unchanged": True,
        "model_restarted": False,
    }
    output = run_dir / "offline-quality-recheck.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if quality["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
