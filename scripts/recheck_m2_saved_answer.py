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
    evaluate_pytest_diagnostic_evidence,
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
    immutable_paths = [
        "ask.stdout.json",
        "m2-real-model-run.json",
        "model-requests.jsonl",
        "model-responses.jsonl",
        "synthetic_repo/failure.log",
        "synthetic_repo/src/totals.py",
        "synthetic_repo/tests/test_totals.py",
    ]
    original_hashes = {
        name: hashlib.sha256((run_dir / name).read_bytes()).hexdigest()
        for name in immutable_paths
        if (run_dir / name).is_file()
    }
    answer = raw.get("answer", "")
    fixture_before = {
        item.get("path"): item.get("sha256")
        for item in run_record.get("fixture_hashes_before", [])
        if isinstance(item, dict)
    }
    fixture_after = {
        item.get("path"): item.get("sha256")
        for item in run_record.get("fixture_hashes_after", [])
        if isinstance(item, dict)
    }
    captured_log = run_dir / "synthetic_repo" / "failure.log"
    captured_log_bytes = captured_log.read_bytes() if captured_log.is_file() else None
    captured_log_sha256 = (
        hashlib.sha256(captured_log_bytes).hexdigest()
        if captured_log_bytes is not None
        else None
    )
    log_matches_run_manifest = (
        captured_log_sha256 is not None
        and captured_log_sha256 == fixture_before.get("failure.log")
        and fixture_before == fixture_after
        and run_record.get("fixture_unchanged") is True
        and len(captured_log_bytes) <= 1024 * 1024
    )
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
            if read.get("full_file_sha256") != fixture_before.get(relative):
                raise ValueError("saved_read_does_not_match_original_fixture_manifest")
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(read["content"].encode("utf-8"))
        if log_matches_run_manifest:
            # The repository read allowlist excludes .log files. Use an
            # offline-only .txt copy with identical bytes; this remains
            # supplemental evidence, never a claim that the model read it.
            (root / "failure.txt").write_bytes(captured_log_bytes)
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
        if log_matches_run_manifest:
            current = workspace.read_text("failure.txt")
            derived_reads.append(
                {
                    "result_kind": "trusted_file_read",
                    "evidence_id": current.evidence_id,
                    "workspace_id": current.workspace_id,
                    "relative_path": current.relative_path,
                    "start_line": current.start_line,
                    "end_line": current.end_line,
                    "line_count": current.line_count,
                    "content": current.content,
                    "full_file_sha256": current.full_file_sha256,
                    "fragment_sha256": current.fragment_sha256,
                    "offline_only_read": True,
                    "source": "saved_run_artifact_not_model_tool_trace",
                }
            )
        citation = validate_answer_citations(answer, derived_reads, workspace=workspace)
        numeric = evaluate_arithmetic_claims(answer, trusted_reads=derived_reads)
        diagnostic = evaluate_pytest_diagnostic_evidence(
            run_record.get("question", ""), answer, derived_reads, citation
        )
        quality = evaluate_answer_quality(
            answer=answer,
            run_status="completed",
            terminal_reason=raw.get("terminal_reason", "final_answer"),
            trace_issues=raw.get("tool_trace_issues", []),
            read_evidence_count=len(derived_reads),
            citation_check=citation,
            numeric_check=numeric,
            diagnostic_check=diagnostic,
        )
        disk_hashes = {
            read["relative_path"]: workspace.read_text(
                read["relative_path"]
            ).full_file_sha256
            for read in derived_reads
        }
    final_hashes = {
        name: hashlib.sha256((run_dir / name).read_bytes()).hexdigest()
        for name in immutable_paths
        if (run_dir / name).is_file()
    }
    result = {
        "kind": "offline_recheck_of_preserved_m2_real_model_answer",
        "reassessment_version": "diagnostic-evidence-v3",
        "run_id": run_id,
        "source_run_record_status": run_record.get("status"),
        "source_ask_status": raw.get("status"),
        "original_ask_exit_code": run_record.get("ask_exit_code"),
        "original_answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
        "citation": citation,
        "numeric": numeric,
        "diagnostic": diagnostic,
        "quality": quality,
        "recorded_read_sha256": {
            read["relative_path"]: read["full_file_sha256"] for read in raw_reads
        },
        "disk_sha256_in_temporary_mirror": disk_hashes,
        "immutable_source_file_sha256_before": original_hashes,
        "immutable_source_file_sha256_after": final_hashes,
        "raw_evidence_unchanged": original_hashes == final_hashes,
        "model_restarted": False,
    }
    result["historical_model_read_paths"] = sorted(
        read["relative_path"] for read in raw_reads
    )
    result["offline_failure_log_artifact"] = {
        "path": "synthetic_repo/failure.log",
        "sha256": captured_log_sha256,
        "matches_saved_fixture_manifest": log_matches_run_manifest,
        "was_read_by_historical_model": False,
        "temporary_read_path": "failure.txt" if log_matches_run_manifest else None,
    }
    output = run_dir / "offline-quality-reassessment-diagnostic-evidence-v3.json"
    if output.exists():
        raise FileExistsError("diagnostic_reassessment_output_already_exists")
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if quality["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
