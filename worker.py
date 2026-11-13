#!/usr/bin/env python3
"""Persistent NDJSON adapter for the transport-independent dice engine."""

from __future__ import annotations

import json
import signal
import sys
from typing import Any

from dicebot import DiceEngine


def validate_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("worker request must be an object")
    if type(payload.get("id")) is not int:
        raise ValueError("worker request id must be an integer")
    for field in ("message", "channel_id", "sender_name"):
        if not isinstance(payload.get(field), str):
            raise ValueError(f"worker request {field} must be a string")
    for field in ("sender_groups", "gm_group_ids"):
        value = payload.get(field)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"worker request {field} must be a list of strings")
    return payload


def handle_request(engine: DiceEngine, request: dict[str, Any]) -> dict[str, Any]:
    engine.group_name_by_id = {
        group_id: engine.gm_group_name for group_id in request["gm_group_ids"]
    }
    client_info = {
        "cid": request["channel_id"],
        "client_servergroups": ",".join(request["sender_groups"]),
    }
    try:
        response = engine._dispatch_message(
            request["message"].strip(), client_info, request["sender_name"]
        )
    except ValueError as exc:
        response = str(exc)
    return {"id": request["id"], "response": response}


def stop_on_sigterm(_signum: int, _frame: Any) -> None:
    raise SystemExit(0)


def main() -> int:
    signal.signal(signal.SIGTERM, stop_on_sigterm)
    try:
        engine = DiceEngine()
        for line in sys.stdin:
            request = validate_request(json.loads(line))
            response = handle_request(engine, request)
            print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), flush=True)
    except Exception as exc:
        print(f"[dicebot-worker] fatal: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
