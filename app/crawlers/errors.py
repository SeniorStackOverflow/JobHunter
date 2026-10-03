from __future__ import annotations


class SourceDegradedError(RuntimeError):
    """A blocked or structurally degraded source must pause automatic actions."""
