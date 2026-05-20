"""Stage 1: Design. See ADR-0004.

Public exports:
    - DesignStageHandler: dispatcher (brownfield-only in Lite)
    - BrownfieldDesignHandler: concrete handler for brownfield tickets
"""

from __future__ import annotations

from ._brownfield import BrownfieldDesignHandler
from ._handler import DesignStageHandler

__all__ = [
    "DesignStageHandler",
    "BrownfieldDesignHandler",
]
