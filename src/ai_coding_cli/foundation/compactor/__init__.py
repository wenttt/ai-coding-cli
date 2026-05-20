"""Compactor (Lite: MicroCompact only). See ADR-0011 + ADR-0030.

Public exports:
    - Compactor: deterministic rule-based compactor
    - CompactorConfig: thresholds
    - CompactionResult: telemetry returned to caller
"""

from __future__ import annotations

from ._compactor import (
    CompactionResult,
    Compactor,
    CompactorConfig,
)

__all__ = [
    "Compactor",
    "CompactorConfig",
    "CompactionResult",
]
