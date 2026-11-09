"""Append-only runtime logging → cache/runtimes.jsonl (+ flushed stdout helpers)."""
import json
import time
from pathlib import Path
from datetime import datetime, timezone

_LOG = Path(__file__).parent.parent / "cache" / "runtimes.jsonl"


def log_runtime(**fields):
    fields["ts"] = datetime.now(timezone.utc).isoformat()
    try:
        _LOG.parent.mkdir(exist_ok=True)
        with open(_LOG, "a") as f:
            f.write(json.dumps(fields, ensure_ascii=False) + "\n")
    except Exception:
        pass


def now() -> float:
    return time.perf_counter()
