from __future__ import annotations

import json
from typing import Any


CONTEXT_SIZE_ESTIMATE_METHOD = "payload_bytes_div_4_estimate"


def measure_context_payload(payload: Any) -> dict[str, Any]:
    serialized = json.dumps(payload, default=str, ensure_ascii=False)
    payload_bytes = len(serialized.encode("utf-8"))
    return {
        "payload_bytes": payload_bytes,
        "token_estimate": max(1, payload_bytes // 4),
        "method": CONTEXT_SIZE_ESTIMATE_METHOD,
        "is_estimate": True,
    }


def annotate_context_metrics(data: dict[str, Any], payload: Any) -> dict[str, Any]:
    annotated = dict(data)
    metrics = measure_context_payload(payload)
    annotated.setdefault("context_payload_bytes", metrics["payload_bytes"])
    annotated.setdefault("context_size_token_estimate", metrics["token_estimate"])
    annotated.setdefault("context_size_method", metrics["method"])
    annotated.setdefault("context_size_is_estimate", metrics["is_estimate"])
    return annotated
