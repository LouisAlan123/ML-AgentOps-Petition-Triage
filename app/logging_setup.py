"""Structured JSON logging with a correlation id (run_id) carried in a contextvar across agent steps (D5)."""
from __future__ import annotations
import contextvars, json, logging, os, sys, time

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")
SERVICE = os.environ.get("SERVICE_NAME", "app")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
             "level": record.levelname, "service": SERVICE, "logger": record.name, "msg": record.getMessage(),
             "correlation_id": correlation_id.get()}
        ef = getattr(record, "extra_fields", None)
        if ef:
            d.update(ef)
        for k, v in record.__dict__.items():  # logger.info(..., extra={"clause": ...})
            if k not in logging.LogRecord("", 0, "", 0, "", (), None).__dict__ and k not in d and k not in ("extra_fields", "message", "asctime"):
                d[k] = v
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, default=str)


_configured = False


def get_logger(name: str) -> logging.Logger:
    global _configured
    if not _configured:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(JsonFormatter())
        root = logging.getLogger()
        root.handlers[:] = [h]
        root.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
        for noisy in ("httpx", "httpcore", "urllib3", "faiss", "faiss.loader", "uvicorn.access"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        _configured = True
    return logging.getLogger(name)
