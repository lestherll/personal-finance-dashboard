"""Logging configuration."""

import logging
import logging.config
from config import LOG_LEVEL

# Logging configuration
LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "[%(asctime)s] %(levelname)s in %(name)s: %(message)s",
        },
        "detailed": {
            "format": "[%(asctime)s] %(levelname)s in %(name)s:%(filename)s:%(funcName)s:%(lineno)d: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "level": LOG_LEVEL,
            "formatter": "default",
            "stream": "ext://sys.stdout",
        },
    },
    "root": {
        "level": LOG_LEVEL,
        "handlers": ["console"],
    },
}


def setup_logging():
    """Initialize logging configuration."""
    logging.config.dictConfig(LOGGING_CONFIG)
    return logging.getLogger(__name__)
