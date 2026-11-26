"""Collect and verify the NW-07 reboot preparation for first-use startup runs."""
from __future__ import annotations

import ctypes
import json
import ntpath
import os
from pathlib import Path
import subprocess
import uuid

from hardware_evidence_host import run_windows_text

PROCESS_CREATION_GUID = "{0cce922b-69ae-11d9-bed3-505054503030}"
# Include the distributed roles and the original linking names to catch pre-runs.
RENDERERS = ("common_baseline.exe", "optional_variant.exe", "fallback_common.exe",
             "canonical_release.exe", "canonical_reference.exe",
             "nw11_common_baseline.exe", "nw11_common_analytic.exe",
             "nw11_optional_mesh.exe", "nw11_optional_ray.exe",
             "nw11_fallback_common.exe", "nw11_fallback_analytic.exe")

_QUERY = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
function Fields($event) {
    $fields = @{}
    foreach ($field in ([xml]$event.ToXml()).Event.EventData.Data) {
        $fields[$field.Name] = $field.InnerText
    }
    return $fields
}
$boot = Get-WinEvent -FilterHashtable @{ LogName='System'; ProviderName='Microsoft-Windows-Kernel-Boot'; Id=27 } -MaxEvents 1
$since = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
$oldest = Get-WinEvent -LogName Security -Oldest -MaxEvents 1
$previous = Get-WinEvent -FilterHashtable @{ LogName='Security'; Id=4688; EndTime=$since } -MaxEvents 1
$names = 'NAMES_PLACEHOLDER'.Split(',')
$disruptions = [Collections.Generic.List[int]]::new()
$launches = [Collections.Generic.List[object]]::new()
Get-WinEvent -FilterHashtable @{ LogName='Security'; Id=4688,4719,1100,1101,1102,1104,1108; StartTime=$since } |
    ForEach-Object {
        if ($_.Id -eq 4688) {
            $fields = Fields $_
            if ([IO.Path]::GetFileName($fields.NewProcessName) -in $names) {
                $launches.Add(@{
                    record_id=$_.RecordId; time=$_.TimeCreated.ToFileTimeUtc()
                    path=$fields.NewProcessName; pid=$fields.NewProcessId
                })
            }
        } elseif ($_.Id -ne 4719 -or (Fields $_).SubcategoryGuid -eq 'GUID_PLACEHOLDER') {
            $disruptions.Add($_.Id)
        }
    }
@{
    boot_type=(Fields $boot).BootType; boot_record=$boot.RecordId
    boot_time=$boot.TimeCreated.ToFileTimeUtc(); system_started=$since.ToFileTimeUtc()
    oldest_security=$oldest.TimeCreated.ToFileTimeUtc()
    previous_process_audit=$previous.TimeCreated.ToFileTimeUtc()
    disruptions=@($disruptions.ToArray()); launches=@($launches.ToArray())
} | ConvertTo-Json -Compress -Depth 4
""".replace("GUID_PLACEHOLDER", PROCESS_CREATION_GUID).replace("NAMES_PLACEHOLDER", ",".join(RENDERERS))


def _audit_flags(system: Path) -> int:
    # AUDIT_POLICY_INFORMATION and AuditQuerySystemPolicy from ntsecapi.h.
    class Guid(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("mid", ctypes.c_uint16),
                    ("high", ctypes.c_uint16), ("rest", ctypes.c_ubyte * 8)]

    class AuditPolicy(ctypes.Structure):
        _fields_ = [("subcategory", Guid), ("flags", ctypes.c_uint32), ("category", Guid)]

    dll = ctypes.WinDLL(str(system / "advapi32.dll"), use_last_error=True)
    query = dll.AuditQuerySystemPolicy
    query.argtypes = [ctypes.POINTER(Guid), ctypes.c_uint32, ctypes.POINTER(ctypes.POINTER(AuditPolicy))]
    query.restype = ctypes.c_ubyte
    free = dll.AuditFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    category = Guid.from_buffer_copy(uuid.UUID(PROCESS_CREATION_GUID).bytes_le)
    policy = ctypes.POINTER(AuditPolicy)()
    if not query(ctypes.byref(category), 1, ctypes.byref(policy)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return policy.contents.flags
    finally:
        free(policy)


def observe() -> dict:
    """Use the kernel boot and Security logs without changing host settings."""
    system = Path(os.environ["SystemRoot"]) / "System32"
    powershell = system / "WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = run_windows_text([str(powershell), "-NoProfile", "-NonInteractive", "-Command", _QUERY],
                                  check=True, timeout=35)
        return {**json.loads(result.stdout), "process_creation_audit_flags": _audit_flags(system)}
    except (OSError, AttributeError, ValueError, subprocess.SubprocessError) as exc:
        return {"unavailable": str(exc)}


def qualify(raw: dict, startup: dict, report: dict, capture_started: int, capture_ended: int) -> dict:
    """Derive first use from the retained audit events, keyed to the exact startup."""
    try:
        start, end = startup["start_filetime"], startup["end_filetime"]
        boot = raw["boot_time"]
        flags = raw["process_creation_audit_flags"]
        if (type(flags) is not int or flags not in (1, 3) or raw["disruptions"] != []
                or raw["boot_type"] not in ("0", "0x0", 0) or type(raw["boot_record"]) is not int
                or raw["boot_record"] <= 0
                or abs(raw["system_started"] - boot) > 60 * 10_000_000
                or not 0 < raw["oldest_security"] <= raw["previous_process_audit"] < raw["system_started"] < start
                or not 0 < boot < start < end <= capture_started <= capture_ended):
            raise ValueError("full reboot or continuous successful process audit was not established")
        executable = ntpath.normcase(ntpath.normpath(startup["command"][0]))
        names = {ntpath.basename(executable)}
        # The roles staged for a bundle can have equivalent original link outputs.
        aliases = {
            "common_baseline.exe": ("nw11_common_baseline.exe", "nw11_common_analytic.exe"),
            "optional_variant.exe": ("nw11_optional_mesh.exe", "nw11_optional_ray.exe"),
            "fallback_common.exe": ("nw11_fallback_common.exe", "nw11_fallback_analytic.exe"),
        }
        names.update(aliases.get(ntpath.basename(executable), ()))
        candidates = [item for item in raw["launches"] if
                      ntpath.basename(ntpath.normcase(item["path"])) in names and item["time"] <= capture_ended]
        event = min(candidates, key=lambda item: item["record_id"])
        if (type(event["record_id"]) is not int or event["record_id"] <= 0
                or sum(item["record_id"] == event["record_id"] for item in candidates) != 1
                or not start <= event["time"] <= end or int(event["pid"], 0) != startup["process_id"]
                or ntpath.normcase(ntpath.normpath(event["path"])) != executable
                or abs(event["time"] - report["process_creation_filetime"]) > 2 * 10_000_000):
            raise ValueError("the first audited launch since reboot is not the bound startup")
        return {"status": "cold_driver_cache", "method": "nw07_full_reboot_first_use",
                "boot_event_record_id": raw["boot_record"], "process_event_record_id": event["record_id"],
                "launch_id": startup["launch_id"], "executable_sha256": startup["executable_sha256"],
                "commit": startup["commit"], "application_pipeline_cache_used": False}
    except (KeyError, ValueError, TypeError, IndexError):
        return {"status": "pending", "reason": "Full reboot with continuous audit and first use could not be verified"}
