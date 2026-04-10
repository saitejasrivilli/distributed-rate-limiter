import logging
import sys
import json
import time
from typing import Any

# ---------------------------------------------------------------------------
# Structured JSON logger — safe for production (no stack traces in responses)
# ---------------------------------------------------------------------------

class JSONFormatter(logging.Formatter):
    """Emit log records as single-line JSON for log aggregators."""

    def format(self, record: logging.LogRecord) -> str:
        log: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            # Include exception type and message but NOT the full traceback
            # in structured fields — the full traceback goes to stderr only.
            exc_type, exc_val, _ = record.exc_info
            log["exc_type"] = exc_type.__name__ if exc_type else None
            log["exc_msg"] = str(exc_val)
        for key in ("client_id", "algorithm", "request_id", "ip", "path", "status"):
            if hasattr(record, key):
                log[key] = getattr(record, key)
        return json.dumps(log)


def setup_logging() -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.handlers = [handler]

    # Silence noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    return logging.getLogger("rate_limiter")


logger = setup_logging()
