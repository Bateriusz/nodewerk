#!/usr/bin/env python3
"""Shared physical GPU identity registry for repository hardware evidence.

The registry is deliberately separate from task-local performance contracts.
Physical validation tasks must use these measured PCI identities instead of
copying model/device constants from older task contracts.
"""

from __future__ import annotations

from typing import Any


PHYSICAL_DEVICE_TIERS: dict[str, dict[str, Any]] = {
    "desktop_ada_representative": {
        "architecture": "Ada",
        "representative_model": "NVIDIA GeForce RTX 4060 Ti",
        "vendor_id": 0x10DE,
        "device_id": 0x2803,
        "device_key": "pci:10de:2803",
        "verified_baseline": "benchmarks/hardware/rtx-4060-baseline.json",
    },
    "desktop_blackwell_representative": {
        "architecture": "Blackwell",
        "representative_model": "NVIDIA GeForce RTX 5070 Ti",
        "vendor_id": 0x10DE,
        "device_id": 0x2C05,
        "device_key": "pci:10de:2c05",
        "verified_baseline": "benchmarks/hardware/rtx-50-series-baseline.json",
    },
}


def device_key(vendor_id: int, device_id: int) -> str:
    if isinstance(vendor_id, bool) or isinstance(device_id, bool):
        raise ValueError("PCI vendor/device identifiers must be integers")
    if not isinstance(vendor_id, int) or not isinstance(device_id, int):
        raise ValueError("PCI vendor/device identifiers must be integers")
    if not 0 <= vendor_id <= 0xFFFF or not 0 <= device_id <= 0xFFFF:
        raise ValueError("PCI vendor/device identifiers must fit 16 bits")
    return f"pci:{vendor_id:04x}:{device_id:04x}"


def physical_device_tier(vendor_id: int, device_id: int) -> str | None:
    key = device_key(vendor_id, device_id)
    return next(
        (
            tier
            for tier, identity in PHYSICAL_DEVICE_TIERS.items()
            if identity["device_key"] == key
        ),
        None,
    )


def nw11_device_tiers() -> dict[str, dict[str, str]]:
    """Return the NW-11 contract projection without repository-only metadata."""
    return {
        tier: {
            "architecture": str(identity["architecture"]),
            "representative_model": str(identity["representative_model"]),
            "device_key": str(identity["device_key"]),
        }
        for tier, identity in PHYSICAL_DEVICE_TIERS.items()
    }
