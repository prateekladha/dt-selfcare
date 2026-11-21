"""Structured JSON logs (one line per event) for OCI Logging / any log pipeline."""
import logging
import sys

import structlog


def configure_logging(service: str, level: str = "INFO") -> structlog.BoundLogger:
    logging.basicConfig(stream=sys.stdout, level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
    )
    return structlog.get_logger(service=service)
