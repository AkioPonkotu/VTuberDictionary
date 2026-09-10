"""Backward-compatible import path for persistence adapters."""

from .infrastructure.persistence import *  # noqa: F403
from .infrastructure.persistence import _write_bytes_atomic  # noqa: F401
