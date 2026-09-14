"""Small, failure-tolerant startup log for packaged builds."""

from __future__ import annotations

import json
import traceback
from datetime import datetime

from runtime_paths import LOG_DIR


def startup_log(event: str, **fields) -> None:
    """Append one JSON line; diagnostics must never affect startup."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": str(event),
            **fields,
        }
        with (LOG_DIR / "startup.log").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def startup_exception(event: str, exc: BaseException, **fields) -> None:
    startup_log(event, error=str(exc), traceback=traceback.format_exc(), **fields)
