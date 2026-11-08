#!/usr/bin/env python3
"""Shared hardware-evidence checks used by schema validation and verification tooling."""

from __future__ import annotations


def desktop_cpu_model_matches(cpu_model: str | None) -> bool:
    """Return True when CPU metadata is actually recorded.

    NW-01 requires recorded CPU metadata, not a specific CPU family or model whitelist.
    Desktop workstations using Ryzen Threadripper, Intel Xeon W, or other non-Core/non-Ryzen
    processors remain eligible.
    """
    return bool(cpu_model and cpu_model.strip())
