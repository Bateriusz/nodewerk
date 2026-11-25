#!/usr/bin/env python3
"""Shared Windows physical-host and hardware-evidence primitives.

This module centralizes lessons learned by NW-07/NW-10 physical validation so
later tasks do not grow task-local VM/VBS detection or locale-dependent Windows
subprocess parsing.  Callers may add task-specific GPU/capability checks, but
physical-host classification remains fail-closed and shared.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
from typing import Any, Mapping, Sequence


VM_MARKERS = (
    "virtual machine",
    "vmware",
    "virtualbox",
    "kvm",
    "qemu",
    "xen",
    "parallels",
    "bochs",
    "openstack",
    "hyper-v",
    "innotek",
    "bhyve",
    "cloud hypervisor",
    "amazon ec2",
    "google compute engine",
    "digitalocean",
)

GENERIC_PRODUCT_MARKERS = (
    "to be filled by o.e.m.",
    "system product name",
    "default string",
    "not specified",
    "not available",
    "oem",
)


def _windows_code_pages() -> tuple[str, ...]:
    """Return explicit Windows OEM/ANSI code pages without Python locale."""
    if os.name != "nt":
        return ()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    pages: list[str] = []
    for getter_name in ("GetOEMCP", "GetACP"):
        getter = getattr(kernel32, getter_name)
        getter.argtypes = []
        getter.restype = ctypes.c_uint
        page = int(getter())
        if page > 0:
            encoding = f"cp{page}"
            if encoding not in pages:
                pages.append(encoding)
    return tuple(pages)


def decode_windows_output(payload: bytes) -> str:
    """Decode Windows tool output deterministically and fail closed."""
    if not payload:
        return ""
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return payload.decode("utf-16")
    if payload.startswith(b"\xef\xbb\xbf"):
        return payload.decode("utf-8-sig")
    # Some redirected Windows tools emit UTF-16LE without a BOM.
    if len(payload) >= 4 and payload[1::2].count(0) * 2 >= len(payload) // 2:
        try:
            return payload.decode("utf-16-le")
        except UnicodeDecodeError:
            pass
    errors: list[str] = []
    for encoding in ("utf-8", *_windows_code_pages()):
        try:
            return payload.decode(encoding)
        except (UnicodeDecodeError, LookupError) as exc:
            errors.append(f"{encoding}: {exc}")
    raise UnicodeError("unable to decode Windows subprocess output; " + "; ".join(errors))


def run_windows_text(
    command: Sequence[str | os.PathLike[str]],
    *,
    timeout: int = 15,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a Windows command while decoding stdout/stderr independent of locale."""
    args = [str(item) for item in command]
    completed = subprocess.run(
        args,
        capture_output=True,
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    stdout = decode_windows_output(completed.stdout)
    stderr = decode_windows_output(completed.stderr)
    result = subprocess.CompletedProcess(args, completed.returncode, stdout, stderr)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, output=result.stdout, stderr=result.stderr
        )
    return result


def windows_virtualization_probe() -> dict[str, Any]:
    """Collect physical-host identity plus Hyper-V/VBS state on Windows."""
    if os.name != "nt":
        return {
            "available": False,
            "manufacturer": "",
            "model": "",
            "hypervisor_present": None,
            "virtualization_based_security_status": None,
            "error": "not_windows",
        }
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            "$ErrorActionPreference='Stop';"
            "$c=Get-CimInstance Win32_ComputerSystem;"
            "$p=$null;try{$p=Get-CimInstance Win32_ComputerSystemProduct -ErrorAction Stop}catch{};"
            "$b=$null;try{$b=Get-CimInstance Win32_BIOS -ErrorAction Stop}catch{};"
            "$m=$null;try{$m=Get-CimInstance Win32_BaseBoard -ErrorAction Stop}catch{};"
            "$vbs=$null;"
            "try{$d=Get-CimInstance -Namespace root\\Microsoft\\Windows\\DeviceGuard "
            "-ClassName Win32_DeviceGuard -ErrorAction Stop;"
            "$vbs=$d.VirtualizationBasedSecurityStatus}catch{};"
            "[pscustomobject]@{Manufacturer=$c.Manufacturer;Model=$c.Model;"
            "ProductVendor=$p.Vendor;ProductName=$p.Name;"
            "BiosManufacturer=$b.Manufacturer;BiosVersion=$b.SMBIOSBIOSVersion;"
            "BaseBoardManufacturer=$m.Manufacturer;BaseBoardProduct=$m.Product;"
            "HypervisorPresent=$c.HypervisorPresent;"
            "VirtualizationBasedSecurityStatus=$vbs}|ConvertTo-Json -Compress"
        ),
    ]
    try:
        completed = run_windows_text(command, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        return {
            "available": False,
            "manufacturer": "",
            "model": "",
            "hypervisor_present": None,
            "virtualization_based_security_status": None,
            "error": str(exc),
        }
    if completed.returncode != 0:
        return {
            "available": False,
            "manufacturer": "",
            "model": "",
            "hypervisor_present": None,
            "virtualization_based_security_status": None,
            "error": completed.stderr.strip() or completed.stdout.strip(),
        }
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {
            "available": False,
            "manufacturer": "",
            "model": "",
            "hypervisor_present": None,
            "virtualization_based_security_status": None,
            "error": str(exc),
        }
    raw_vbs_status = value.get("VirtualizationBasedSecurityStatus")
    try:
        vbs_status = int(raw_vbs_status) if raw_vbs_status is not None else None
    except (TypeError, ValueError):
        vbs_status = None
    raw_hypervisor_present = value.get("HypervisorPresent")
    hypervisor_present = (
        raw_hypervisor_present if isinstance(raw_hypervisor_present, bool) else None
    )
    return {
        "available": True,
        "manufacturer": str(value.get("Manufacturer") or ""),
        "model": str(value.get("Model") or ""),
        "product_vendor": str(value.get("ProductVendor", "") or ""),
        "product_name": str(value.get("ProductName", "") or ""),
        "bios_manufacturer": str(value.get("BiosManufacturer", "") or ""),
        "bios_version": str(value.get("BiosVersion", "") or ""),
        "baseboard_manufacturer": str(value.get("BaseBoardManufacturer", "") or ""),
        "baseboard_product": str(value.get("BaseBoardProduct", "") or ""),
        "hypervisor_present": hypervisor_present,
        "virtualization_based_security_status": vbs_status,
        "error": "",
    }


def evaluate_physical_virtualization_probe(
    probe: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Evaluate one Windows physical-host probe with the NW-07/NW-10 policy."""
    if not isinstance(probe, Mapping):
        return {
            "valid": False,
            "available": False,
            "hypervisor_measured": False,
            "virtualization_indicators": [],
            "generic_identity": False,
            "identity_complete": False,
            "hypervisor_explained_by_vbs": False,
            "reasons": ["virtualization probe is absent"],
        }
    identity_fields = (
        "manufacturer",
        "model",
        "product_vendor",
        "product_name",
        "bios_manufacturer",
        "bios_version",
        "baseboard_manufacturer",
        "baseboard_product",
    )
    identity_text = " ".join(
        str(probe.get(field, "") or "") for field in identity_fields
    ).lower()
    indicators = sorted(marker for marker in VM_MARKERS if marker in identity_text)
    physical_identity_fields = (
        probe.get("manufacturer"),
        probe.get("model"),
        probe.get("product_vendor"),
        probe.get("product_name"),
        probe.get("bios_manufacturer"),
        probe.get("baseboard_manufacturer"),
    )
    identity_complete = all(
        bool(str(value or "").strip()) for value in physical_identity_fields
    )
    product_text = " ".join(
        str(probe.get(field, "") or "")
        for field in ("product_name", "baseboard_product", "bios_version")
    ).lower()
    generic_identity = any(
        marker == product_text.strip() or marker in product_text
        for marker in GENERIC_PRODUCT_MARKERS
    )
    hypervisor_present = probe.get("hypervisor_present")
    hypervisor_measured = isinstance(hypervisor_present, bool)
    vbs_status = probe.get("virtualization_based_security_status")
    explained_vbs = (
        hypervisor_present is True
        and vbs_status == 2
        and not indicators
        and identity_complete
        and not generic_identity
    )
    available = probe.get("available") is True
    valid = (
        available
        and hypervisor_measured
        and bool(str(probe.get("manufacturer") or "").strip())
        and bool(str(probe.get("model") or "").strip())
        and not indicators
        and not generic_identity
        and (hypervisor_present is False or explained_vbs)
    )
    reasons: list[str] = []
    if not available:
        reasons.append("virtualization probe is unavailable")
    if not hypervisor_measured:
        reasons.append("hypervisor status was not measured")
    if not str(probe.get("manufacturer") or "").strip() or not str(
        probe.get("model") or ""
    ).strip():
        reasons.append("manufacturer/model identity is incomplete")
    if indicators:
        reasons.append("VM markers: " + ", ".join(indicators))
    if generic_identity:
        reasons.append("generic physical product identity")
    if hypervisor_present is True and not explained_vbs:
        reasons.append("active hypervisor is not independently explained by VBS")
    return {
        "valid": valid,
        "available": available,
        "hypervisor_measured": hypervisor_measured,
        "virtualization_indicators": indicators,
        "generic_identity": generic_identity,
        "identity_complete": identity_complete,
        "hypervisor_explained_by_vbs": explained_vbs,
        "reasons": reasons,
    }


def parse_pci_hex(value: Any) -> int:
    """Parse NVIDIA/DXGI hexadecimal identifiers with or without a 0x prefix."""
    text = str(value or "").strip()
    if not text:
        raise ValueError("missing PCI identifier")
    if text.lower().startswith("0x"):
        text = text[2:]
    if not text or any(ch not in "0123456789abcdefABCDEF" for ch in text):
        raise ValueError("malformed PCI identifier")
    return int(text, 16)
