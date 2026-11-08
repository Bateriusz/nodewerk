#!/usr/bin/env python3
"""Shared hardware-evidence checks used by schema validation and verification tooling."""

from __future__ import annotations

def desktop_cpu_model_matches(cpu_model: str | None) -> bool:
    """Return True when a non-generic CPU model string is recorded.

    NW-01 requires recorded CPU metadata, not CPU-family or desktop/mobile classification.
    GPU tier eligibility is determined from the observed GPU, not from the CPU. This check
    rejects only missing and generic host strings that do not identify a CPU model.
    """
    if not cpu_model:
        return False
    lower = cpu_model.strip().lower()
    if not lower:
        return False
    generic = {
        "amd64",
        "x86_64",
        "x64",
        "arm64",
        "intel64",
        "unknown",
        "unverified",
        "none",
    }
    if lower in generic:
        return False
    if lower.startswith(("intel64 family", "amd64 family")):
        return False
    return True
