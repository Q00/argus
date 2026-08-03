"""Stdio JSON-RPC MCP server exposing the review checklist.

Lets an agent session inspect and drive an active run: status, concern
listing, lease-based claiming, evidence-backed resolution, attrition, and
physical completion verification. State discovery is repo-local via the
``.argus/active.json`` pointer in the working directory.
"""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
import sys
import traceback
from typing import Any

from argus.core import ArgusError, stable_id
from argus.ledger import TERMINAL_CONCERN_STATES, Ledger
from argus.pipeline import read_active_pointer

SERVER_INFO = {"name": "argus-review-checklist", "version": "0.1.0"}
PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "review_health",
        "description": "Verify that the repo-local argus checklist MCP server is running.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "review_status",
        "description": "Return completion counts and the next required action for the active review.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "review_list_concerns",
        "description": "List concerns from the active review, optionally filtered by status.",
        "inputSchema": {
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "review_claim_next",
        "description": "Lease the next open or expired concern for evidence-based adjudication.",
        "inputSchema": {
            "type": "object",
            "required": ["owner"],
            "properties": {
                "owner": {"type": "string", "minLength": 1},
                "lease_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "review_resolve_concern",
        "description": "Resolve a leased concern into one permitted terminal state with evidence.",
        "inputSchema": {
            "type": "object",
            "required": ["concern_id", "owner", "status", "evidence", "source", "materiality"],
            "properties": {
                "concern_id": {"type": "string"},
                "owner": {"type": "string"},
                "status": {"type": "string", "enum": sorted(TERMINAL_CONCERN_STATES)},
                "evidence": {"type": "string", "minLength": 1},
                "source": {"type": "string", "minLength": 1},
                "materiality": {"type": "string", "enum": ["minor", "major"]},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "review_attrition",
        "description": "Report where concerns died and why (recall observability).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "review_verify_completion",
        "description": "Physically verify that every unit, claim, concern, and final review is complete.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def _state_root() -> Path:
    return Path.cwd() / ".argus"


def _active_ledger() -> tuple[Ledger, str]:
    active = read_active_pointer(_state_root())
    if active is None:
        raise ArgusError("no active argus review")
    ledger_path = Path(active["ledger_path"])
    if not ledger_path.is_file():
        raise ArgusError(f"active ledger is missing: {ledger_path}")
    return Ledger(ledger_path), active["run_id"]


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "review_health":
        return {"ok": True, "server": SERVER_INFO}
    ledger, run_id = _active_ledger()
    if name in {"review_status", "review_verify_completion"}:
        return ledger.completion(run_id).as_dict()
    if name == "review_attrition":
        return ledger.attrition(run_id)
    if name == "review_list_concerns":
        status = arguments.get("status")
        concerns = ledger.list_concerns(run_id)
        if status is not None:
            concerns = [concern for concern in concerns if concern["status"] == status]
        return {"run_id": run_id, "count": len(concerns), "concerns": concerns}
    if name == "review_claim_next":
        concern = ledger.claim_next(
            run_id, arguments["owner"], int(arguments.get("lease_seconds", 300))
        )
        return {"run_id": run_id, "concern": concern}
    if name == "review_resolve_concern":
        resolution_key = stable_id(
            "mcp-resolution",
            arguments["concern_id"],
            arguments["status"],
            arguments["evidence"],
            arguments["source"],
        )
        ledger.resolve_concern(
            arguments["concern_id"],
            arguments["owner"],
            arguments["status"],
            arguments["evidence"],
            arguments["source"],
            arguments["materiality"],
            resolution_key,
        )
        return {
            "resolved": True,
            "concern_id": arguments["concern_id"],
            "resolution_key": resolution_key,
        }
    raise ArgusError(f"unknown MCP tool: {name}")


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    request_id = request.get("id")
    method = request.get("method")
    if method == "initialize":
        requested = request.get("params", {}).get("protocolVersion")
        result: dict[str, Any] = {
            "protocolVersion": requested or PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = request.get("params") or {}
        value = call_tool(params.get("name", ""), params.get("arguments") or {})
        result = {
            "content": [{"type": "text", "text": json.dumps(value, sort_keys=True)}],
            "structuredContent": value,
            "isError": False,
        }
    elif method and method.startswith("notifications/"):
        return None
    else:
        raise ArgusError(f"unsupported JSON-RPC method: {method}")
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _messages(stream: Any) -> Iterator[tuple[dict[str, Any], str]]:
    while True:
        line = stream.readline()
        if not line:
            return
        decoded = line.decode("utf-8") if isinstance(line, bytes) else line
        if not decoded.strip():
            continue
        if decoded.lower().startswith("content-length:"):
            length = int(decoded.split(":", 1)[1].strip())
            while True:
                header = stream.readline()
                if not header or header in {b"\r\n", b"\n", "\r\n", "\n"}:
                    break
            payload = stream.read(length)
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8")
            yield json.loads(payload), "framed"
        else:
            yield json.loads(decoded), "line"


def _write(stream: Any, response: dict[str, Any], mode: str) -> None:
    payload = json.dumps(response, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if mode == "framed":
        stream.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload)
    else:
        stream.write(payload + b"\n")
    stream.flush()


def serve(stdin: Any = None, stdout: Any = None) -> None:
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    for request, mode in _messages(stdin):
        try:
            response = handle(request)
        except Exception as exc:
            request_id = request.get("id") if isinstance(request, dict) else None
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": str(exc)},
            }
            traceback.print_exc(file=sys.stderr)
        if response is not None:
            _write(stdout, response, mode)


def main() -> int:
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
