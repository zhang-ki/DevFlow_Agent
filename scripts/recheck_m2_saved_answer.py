"""Offline-only re-evaluation of the preserved M2 real-model answer."""

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
)
from devflow_agent.real_readonly_agent import validate_answer_citations
from devflow_agent.repository import AuthorizedWorkspace


def main() -> int:
    project = Path(__file__).resolve().parents[1]
    run_id = "20261023T-m2-qwen35-4b-r6-ctx4096"
    run_dir = project / "reports" / "m2_real_model_runs" / run_id
    raw = json.loads((run_dir / "ask.stdout.json").read_text(encoding="utf-8"))
    run_record = json.loads(
        (run_dir / "m2-real-model-run.json").read_text(encoding="utf-8")
    )
    answer = raw.get("answer", "")
    raw_reads = [
        item["result"]
        for item in raw.get("tool_trace", [])
        if item.get("tool") == "repository_readonly_read"
        and item.get("status") == "completed"
        and item.get("result", {}).get("result_kind") == "trusted_file_read"
    ]
    with tempfile.TemporaryDirectory(prefix="devflow-m2-recheck-") as temporary:
        root = Path(temporary)
        derived_reads = []
        seen_paths = set()
        for read in raw_reads:
            relative = read["relative_path"]
            if relative in seen_paths:
                raise ValueError("duplicate_read_path_in_saved_evidence")
            seen_paths.add(relative)
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(read["content"].encode("utf-8"))
        workspace = AuthorizedWorkspace(root)
        for read in raw_reads:
            current = workspace.read_text(
                read["relative_path"],
                start_line=read["start_line"],
                end_line=read["end_line"],
            )
            derived = copy.deepcopy(read)
            derived["workspace_id"] = workspace.workspace_id
            derived["full_file_sha256"] = current.full_file_sha256
            derived["fragment_sha256"] = current.fragment_sha256
            derived["content"] = current.content
            derived_reads.append(derived)
        citation = validate_answer_citations(answer, derived_reads, workspace=workspace)
        numeric = evaluate_arithmetic_claims(answer, trusted_reads=derived_reads)
        quality = evaluate_answer_quality(
            answer=answer,
            run_status="completed",
            terminal_reason=raw.get("terminal_reason", "final_answer"),
            trace_issues=raw.get("tool_trace_issues", []),
            read_evidence_count=len(derived_reads),
            citation_check=citation,
            numeric_check=numeric,
        )
        disk_hashes = {
            read["relative_path"]: workspace.read_text(
                read["relative_path"]
            ).full_file_sha256
            for read in derived_reads
        }
    result = {
        "kind": "offline_recheck_of_preserved_m2_real_model_answer",
        "run_id": run_id,
        "source_run_record_status": run_record.get("status"),
        "source_ask_status": raw.get("status"),
        "original_ask_exit_code": run_record.get("ask_exit_code"),
        "original_answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
        "citation": citation,
        "numeric": numeric,
        "quality": quality,
        "recorded_read_sha256": {
            read["relative_path"]: read["full_file_sha256"] for read in raw_reads
        },
        "disk_sha256_in_temporary_mirror": disk_hashes,
        "raw_evidence_unchanged": True,
        "model_restarted": False,
    }
    output = run_dir / "offline-quality-reassessment-static-numeric.json"
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if quality["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
