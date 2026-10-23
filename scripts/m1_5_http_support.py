"""Evidence-only support for one bounded M1.5 local acceptance; no fake responses."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


BASE = "http://127.0.0.1:11434"
ALIAS = "devflow-qwen35-4b"
# The runtime path is supplied by the caller so machine-specific directories
# are not published in the repository.
RUNTIME = Path(os.environ.get("DEVFLOW_LLAMA_SERVER", "llama-server.exe"))


def utc():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def append(path, value):
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()


def exception_info(exc):
    chain, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(
            {
                "type": type(current).__module__ + "." + type(current).__name__,
                "message": str(current)[:2000],
                "errno": getattr(current, "errno", None),
                "winerror": getattr(current, "winerror", None),
            }
        )
        current = (
            current.__cause__ or current.__context__ or getattr(current, "reason", None)
        )
        if not isinstance(current, BaseException):
            break
    return {
        "chain": chain,
        "traceback": "".join(traceback.format_exception(exc))[-12000:],
    }


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_opener():
    # An explicit empty proxy map ignores all process/system proxy settings.
    return build_opener(ProxyHandler({}), NoRedirect())


def request_json(opener, path, timeout=2.0):
    if path not in ("/health", "/v1/models"):
        raise ValueError("unsupported_acceptance_endpoint")
    started = time.monotonic()
    event = {
        "started_at_utc": utc(),
        "monotonic_start": started,
        "url": BASE + path,
        "proxy_mode": "explicit_empty_ProxyHandler",
        "http_status": None,
        "body": None,
        "error": None,
    }
    try:
        with opener.open(
            Request(BASE + path, headers={"Accept": "application/json"}),
            timeout=timeout,
        ) as response:
            event["http_status"] = response.status
            raw = response.read(1048577)
            if len(raw) > 1048576:
                raise ValueError("response_body_limit_exceeded")
            event["body"] = raw.decode("utf-8")
            event["json"] = json.loads(event["body"])
    except HTTPError as exc:
        event["http_status"] = exc.code
        event["body"] = exc.read(4096).decode("utf-8", errors="replace")
        event["error"] = exception_info(exc)
        exc.close()
    except Exception as exc:
        event["error"] = exception_info(exc)
    event["ended_at_utc"] = utc()
    event["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return event


def check_version(run_dir, runtime=RUNTIME):
    started = time.monotonic()
    record = {
        "argv": [str(runtime), "--version"],
        "started_at_utc": utc(),
        "timeout_seconds": 10,
        "exit_code": None,
        "status": "FAIL",
    }
    try:
        completed = subprocess.run(
            record["argv"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        (run_dir / "runtime-version.stdout.log").write_text(
            completed.stdout, encoding="utf-8"
        )
        (run_dir / "runtime-version.stderr.log").write_text(
            completed.stderr, encoding="utf-8"
        )
        record.update(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        record["status"] = (
            "PASS"
            if completed.returncode == 0
            and "11424" in completed.stdout + completed.stderr
            else "FAIL"
        )
    except Exception as exc:
        record["error"] = exception_info(exc)
    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    save(run_dir / "runtime-version.json", record)
    return record


def check_health(run_dir, *, opener=None, budget_seconds=75, poll_seconds=1):
    opener = opener or local_opener()
    started = time.monotonic()
    deadline = started + budget_seconds
    result = {
        "health": "FAIL",
        "models": "NOT_RUN",
        "models_match_alias": False,
        "budget_seconds": budget_seconds,
        "attempts": [],
    }
    while time.monotonic() < deadline:
        event = request_json(
            opener, "/health", timeout=min(2.0, max(0.01, deadline - time.monotonic()))
        )
        event["attempt"] = len(result["attempts"]) + 1
        result["attempts"].append(event)
        append(run_dir / "health-attempts.jsonl", event)
        if (
            event["http_status"] == 200
            and not event["error"]
            and event.get("json", {}).get("status") == "ok"
        ):
            result["health"] = "PASS"
            break
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(poll_seconds, remaining))
    result["health_elapsed_seconds"] = round(time.monotonic() - started, 3)
    if result["health"] == "PASS":
        event = request_json(opener, "/v1/models", timeout=5.0)
        result["models_response"] = event
        data = event.get("json", {}).get("data", [])
        result["model_ids"] = [
            item.get("id") for item in data if isinstance(item, dict)
        ]
        result["models_match_alias"] = ALIAS in result["model_ids"]
        result["models"] = (
            "PASS"
            if event["http_status"] == 200
            and not event["error"]
            and result["models_match_alias"]
            else "FAIL"
        )
    save(run_dir / "server-health-python.json", result)
    return result


def ask_worker(run_dir, cli_args):
    # Capture actual HTTP request/response bodies, never auth headers/config.
    # All bytes still go through the real httpx transport and real CLI.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import httpx

    from devflow_agent.cli import main

    original = httpx.AsyncClient.send
    counter = 0

    async def observed_send(self, request, *args, **kwargs):
        nonlocal counter
        if str(request.url) != BASE + "/v1/chat/completions":
            raise RuntimeError("unexpected_model_request_endpoint")
        counter += 1
        event = {
            "request_id": counter,
            "started_at_utc": utc(),
            "monotonic_start": time.monotonic(),
            "method": request.method,
            "url": str(request.url),
            "request_json": json.loads(request.content),
        }
        append(run_dir / "model-requests.jsonl", event)
        started = time.monotonic()
        try:
            response = await original(self, request, *args, **kwargs)
            # OpenManus ask_tool uses non-streaming completion requests.
            body = await response.aread()
            result = {
                "request_id": counter,
                "http_status": response.status_code,
                "response_json": json.loads(body),
                "ended_at_utc": utc(),
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
            append(run_dir / "model-responses.jsonl", result)
            return response
        except BaseException as exc:
            append(
                run_dir / "model-responses.jsonl",
                {
                    "request_id": counter,
                    "ended_at_utc": utc(),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "error": exception_info(exc),
                },
            )
            raise

    httpx.AsyncClient.send = observed_send
    try:
        return main(["ask", *cli_args])
    finally:
        httpx.AsyncClient.send = original


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["version", "health", "ask"])
    parser.add_argument("--run-dir", type=Path, required=True)
    args, remainder = parser.parse_known_args()
    if args.mode == "ask":
        return ask_worker(args.run_dir, remainder)
    if remainder:
        parser.error("unexpected arguments")
    result = (
        check_version(args.run_dir)
        if args.mode == "version"
        else check_health(args.run_dir)
    )
    print(json.dumps(result, ensure_ascii=False))
    return (
        0
        if result.get("status") == "PASS"
        or (result.get("health") == result.get("models") == "PASS")
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
