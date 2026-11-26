"""Shared source-input provenance helpers.

Historical equality and current input recording are different assertions.
Consumers must also compare the measured digest and artifact identities.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Mapping


def has_recorded_inputs(record: Mapping[str, Any]) -> bool:
    """Read new reports, or legacy reports that matched their historical pin.

    An explicitly false new field must never fall back to the legacy field.
    """
    if not isinstance(record, Mapping):
        return False
    if "source_inputs_recorded" in record:
        return record["source_inputs_recorded"] is True
    return record.get("matches_frozen_digest") is True


def source_revision(root: Path) -> dict[str, Any]:
    """Record the checked-out revision and whether tracked files are clean."""
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        tree = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=root, text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root, text=True, stderr=subprocess.DEVNULL,
        )
        return {"commit": head, "tree": tree, "tracked_files_clean": not bool(dirty.strip())}
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "tree": None, "tracked_files_clean": None}
