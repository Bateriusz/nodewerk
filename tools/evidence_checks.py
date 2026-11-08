#!/usr/bin/env python3
"""Shared hardware-evidence checks used by schema validation and verification tooling."""

from __future__ import annotations

import re

_MOBILE_CPU_MARKERS = ("laptop", "mobile", "notebook", "tablet")
_MOBILE_INTEL_SUFFIXES = {"p", "u", "h", "hs", "hx", "g1", "g2", "g4", "g7", "g8"}
_MOBILE_AMD_SUFFIXES = {"u", "h", "hs", "hx", "c", "e"}


def desktop_cpu_model_matches(cpu_model: str | None) -> bool:
    """Return True only for a recognized desktop-class CPU model string.

    The NW-01 desktop tiers require a desktop-class processor. CPU strings are operator-
    supplied or host-detected text, so this is a conservative filter: known mobile suffixes
    (Intel P/U/H/HX/HS and AMD U/H/HS/HX) and explicit mobile markers fail certification.
    A recognized desktop CPU model family is required; generic strings such as
    "Intel64 Family 6 Model ..." and missing CPU evidence are not eligible.
    """
    if not cpu_model:
        return False
    lower = cpu_model.strip().lower()
    if not lower or any(marker in lower for marker in _MOBILE_CPU_MARKERS):
        return False
    if any(marker in lower for marker in ("atom", "celeron", "pentium", "ryzen ai")):
        return False
    intel = re.search(r"\bi[3579]-\d{4,5}([a-z][a-z0-9]{0,3})?\b", lower)
    if intel:
        return (intel.group(1) or "") not in _MOBILE_INTEL_SUFFIXES
    ultra = re.search(r"\bcore\s+ultra\s+[3579]\s+\d{3}([a-z][a-z0-9]{0,3})?\b", lower)
    if ultra:
        return (ultra.group(1) or "") not in _MOBILE_INTEL_SUFFIXES
    amd = re.search(r"\bryzen\s+[3579]\s+\d{4}([a-z][a-z0-9]{0,3})?\b", lower)
    if amd:
        return (amd.group(1) or "") not in _MOBILE_AMD_SUFFIXES
    return False
