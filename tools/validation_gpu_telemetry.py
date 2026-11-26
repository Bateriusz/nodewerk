"""Observe the NW-07 host and operating conditions for an NW-11 launch."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Mapping
import xml.etree.ElementTree as ET

import hardware_evidence_host as hardware_host

MEASURED_APPLICATIONS = ("common_baseline.exe", "optional_variant.exe", "fallback_common.exe",
                         "canonical_release.exe", "canonical_reference.exe")


def _power_profiles(system: Path, executables: Path) -> dict[str, dict]:
    """Read the effective PREFERRED_PSTATE setting from the installed NVIDIA driver."""
    # NVDRS_SETTING_V1 and NVDRS_APPLICATION_V4 from NVIDIA's public NVAPI ABI.
    u32, handle = ctypes.c_uint32, ctypes.c_void_p
    unicode_string = ctypes.c_uint16 * 2048

    class Setting(ctypes.Structure):
        _pack_ = 4
        _fields_ = [("version", u32), ("name", unicode_string), ("setting_id", u32),
                    ("value_type", u32), ("location", u32), ("predefined_flags", u32 * 2),
                    ("predefined", u32 * 1025), ("current", u32 * 1025)]

    class Application(ctypes.Structure):
        _fields_ = [("version", u32), ("predefined", u32), ("app_name", unicode_string),
                    ("friendly_name", unicode_string), ("launcher", unicode_string),
                    ("file_in_folder", unicode_string), ("flags", u32), ("command_line", unicode_string)]

    class SettingValues(ctypes.Structure):
        _pack_ = 4
        _fields_ = [("version", u32), ("count", u32), ("value_type", u32),
                    ("default", u32 * 1025), ("values", (u32 * 1025) * 100)]

    library = ctypes.CDLL(str(system / "nvapi64.dll"))
    query = library.nvapi_QueryInterface
    query.argtypes = [u32]
    query.restype = handle

    def bind(code: int, *types: Any):
        address = query(code)
        if not address:
            raise ValueError(f"NVIDIA driver profile interface {code:#x} is unavailable")
        return ctypes.CFUNCTYPE(ctypes.c_int32, *types)(address)

    def checked(status: int) -> None:
        if status != 0:
            raise ValueError(f"NVIDIA driver profile query returned {status}")

    create = bind(0x0694D52E, ctypes.POINTER(handle))
    destroy = bind(0xDAD9CFF8, handle)
    load = bind(0x375DBD6B, handle)
    global_profile_query = bind(0x617BFF9F, handle, ctypes.POINTER(handle))
    base_profile_query = bind(0xDA8466A0, handle, ctypes.POINTER(handle))
    application_profile = bind(0xEEE566B2, handle, ctypes.c_wchar_p, ctypes.POINTER(handle),
                               ctypes.POINTER(Application))
    get_setting = bind(0x73BF8338, handle, handle, u32, ctypes.POINTER(Setting))
    value_list = bind(0x2EC39F90, u32, ctypes.POINTER(u32), ctypes.POINTER(SettingValues))
    power_mode_id = 0x1057EB71
    session = handle()
    checked(bind(0x0150E828)())
    checked(create(ctypes.byref(session)))
    modes = {}
    try:
        checked(load(session))
        global_profile, base_profile = handle(), handle()
        checked(global_profile_query(session, ctypes.byref(global_profile)))
        checked(base_profile_query(session, ctypes.byref(base_profile)))
        for name in MEASURED_APPLICATIONS:
            application, profile = Application(), handle()
            application.version = ctypes.sizeof(application) | (4 << 16)
            found = application_profile(session, (executables / name).as_posix(),
                                        ctypes.byref(profile), ctypes.byref(application))
            if found != -166:  # NVAPI_EXECUTABLE_NOT_FOUND
                checked(found)
            setting = Setting()
            for candidate in (profile, global_profile, base_profile):
                if candidate:
                    setting.version = ctypes.sizeof(setting) | (1 << 16)
                    status = get_setting(session, candidate, power_mode_id, ctypes.byref(setting))
                    if status != -160:  # NVAPI_SETTING_NOT_FOUND
                        checked(status)
                        break
            else:
                values = SettingValues()
                values.version = ctypes.sizeof(values) | (1 << 16)
                count = u32(len(values.values))
                checked(value_list(power_mode_id, ctypes.byref(count), ctypes.byref(values)))
                if values.value_type != 0:
                    raise ValueError("NVIDIA default power management setting is not a DWORD")
                modes[name] = {"value": values.default[0], "location": 3}
                continue
            if setting.setting_id != power_mode_id or setting.value_type != 0:
                raise ValueError("NVIDIA power management setting is not a DWORD")
            modes[name] = {"value": setting.current[0], "location": setting.location}
    finally:
        destroy(session)
    return modes


def _system() -> Path:
    return Path(os.environ["SystemRoot"]) / "System32"


def _run(exe: Path, *args: str, timeout: int = 10) -> str:
    return hardware_host.run_windows_text(
        [exe, *args], timeout=timeout, check=True
    ).stdout


def observe_operating(executables: Path) -> dict[str, Any]:
    """Read matching controls and instantaneous clock/thermal telemetry."""
    system = _system()
    try:
        nv_locations = (system / "nvidia-smi.exe",
                        Path(os.environ.get("ProgramW6432", "C:/Program Files")) /
                        "NVIDIA Corporation/NVSMI/nvidia-smi.exe")
        nv_smi = next(path for path in nv_locations if path.is_file())
        return {
            "power_plan": _run(system / "powercfg.exe", "/getactivescheme"),
            "pcie_power": _run(system / "powercfg.exe", "/query", "SCHEME_CURRENT", "SUB_PCIEXPRESS", "ASPM"),
            "nvidia_smi_xml": _run(nv_smi, "-q", "-x"),
            "nvidia_power_profiles": _power_profiles(system, executables),
        }
    except (OSError, AttributeError, ValueError, StopIteration, subprocess.SubprocessError) as exc:
        return {"unavailable": str(exc)}


def observe(executables: Path) -> dict[str, Any]:
    """Collect OS and NVIDIA driver telemetry using installed runtime tools."""
    system = _system()
    result = observe_operating(executables)
    try:
        query = """
$ErrorActionPreference = 'Stop'
@{
  system = Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model,HypervisorPresent,PCSystemType,TotalPhysicalMemory,NumberOfLogicalProcessors
  processor = @(Get-CimInstance Win32_Processor | Select-Object Name,Manufacturer,NumberOfCores)
  motherboard = @(Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer,Product)
} | ConvertTo-Json -Compress -Depth 4
"""
        result["cim"] = json.loads(_run(system / "WindowsPowerShell/v1.0/powershell.exe",
                                       "-NoProfile", "-NonInteractive", "-Command", query))
        result["virtualization_probe"] = hardware_host.windows_virtualization_probe()
        with tempfile.TemporaryDirectory(prefix="nw11-dxdiag-") as directory:
            xml = Path(directory) / "dxdiag.xml"
            _run(system / "dxdiag.exe", "/whql:off", "/x", str(xml), timeout=60)
            result["display_devices"] = [
                {child.tag: child.text for child in item
                 if child.tag in {"DeviceID", "VendorID", "SubSysID", "DriverModel"}}
                for item in ET.parse(xml).findall("./DisplayDevices/DisplayDevice")
            ]
    except (OSError, AttributeError, ValueError, StopIteration, subprocess.SubprocessError, ET.ParseError) as exc:
        result["unavailable"] = str(exc)
    return result


def _text(value: Any) -> str:
    if not isinstance(value, str) or value.strip().lower() in {
        "", "unknown", "unavailable", "n/a", "[n/a]", "not supported", "default string",
        "to be filled by o.e.m.",
    }:
        raise ValueError("missing host telemetry")
    return value.strip()


def _observed_number(value: str, suffix: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*" + suffix, _text(value))
    if not match:
        raise ValueError("malformed GPU telemetry")
    return float(match[1])


def _gpu_ceiling_power_limit(gpu: ET.Element) -> float:
    """Read the GPU ceiling limit across old and new nvidia-smi XML layouts."""
    containers = [
        gpu.find("gpu_power_readings"),
        gpu.find("power_readings"),
    ]
    preferred_tags = (
        "current_power_limit",
        "enforced_power_limit",
        "power_limit",
    )
    for container in containers:
        if container is None:
            continue
        descendants = list(container.iter())
        for tag in preferred_tags:
            for node in descendants:
                if node.tag == tag and node.text:
                    try:
                        return _observed_number(node.text, "W")
                    except ValueError:
                        continue
    raise ValueError("GPU ceiling power limit is unavailable")


def _qualify_impl(raw: Mapping[str, Any], adapter: Mapping[str, Any]) -> dict[str, Any]:
    """Return stable bound provenance or raise with the exact failed predicate."""
    if "unavailable" in raw:
        raise ValueError(f"host observation unavailable: {raw.get('unavailable')}")
    probe = raw.get("virtualization_probe")
    host_evaluation = hardware_host.evaluate_physical_virtualization_probe(probe)
    if host_evaluation["valid"] is not True:
        detail = "; ".join(host_evaluation.get("reasons") or ["shared physical-host policy rejected the host"])
        raise ValueError(f"physical-host qualification failed: {detail}")
    cim = raw["cim"]
    system = cim["system"]
    manufacturer, model = _text(system["Manufacturer"]), _text(system["Model"])
    hypervisor_present = system["HypervisorPresent"]
    if type(hypervisor_present) is not bool:
        raise ValueError("Win32_ComputerSystem.HypervisorPresent is not a measured boolean")
    if system["PCSystemType"] not in (1, 3):
        raise ValueError(f"Win32_ComputerSystem.PCSystemType={system['PCSystemType']} is not a desktop/workstation")
    if hypervisor_present is not probe.get("hypervisor_present"):
        raise ValueError("CIM and shared virtualization probe disagree on HypervisorPresent")
    if manufacturer.casefold() != _text(probe.get("manufacturer")).casefold():
        raise ValueError("CIM and shared virtualization probe disagree on system manufacturer")
    if model.casefold() != _text(probe.get("model")).casefold():
        raise ValueError("CIM and shared virtualization probe disagree on system model")

    processors = [{"model": _text(item["Name"]), "physical_cores": item["NumberOfCores"]}
                  for item in cim["processor"]]
    board = [{"manufacturer": _text(item["Manufacturer"]), "product": _text(item["Product"])}
             for item in cim["motherboard"]]
    memory, logical = system["TotalPhysicalMemory"], system["NumberOfLogicalProcessors"]
    if not processors:
        raise ValueError("processor inventory is empty")
    if not board:
        raise ValueError("motherboard inventory is empty")
    if type(memory) is not int or memory <= 0:
        raise ValueError("physical memory inventory is unavailable")
    if type(logical) is not int or logical <= 0:
        raise ValueError("logical processor inventory is unavailable")
    if any(type(item["physical_cores"]) is not int or item["physical_cores"] <= 0 for item in processors):
        raise ValueError("physical core inventory is unavailable")

    plan = re.search(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b",
                     raw["power_plan"], flags=re.IGNORECASE)
    if plan is None:
        raise ValueError("active Windows power-plan GUID is unavailable")
    pcie = re.findall(r":\s*(0x[0-9a-f]{8})\s*$", raw["pcie_power"],
                      flags=re.MULTILINE | re.IGNORECASE)
    if len(pcie) < 2:
        raise ValueError("PCIe AC/DC power-management values are unavailable")

    root = ET.fromstring(raw["nvidia_smi_xml"])
    device_id, vendor_id = adapter["device_id"], adapter["vendor_id"]
    query_pci = (device_id << 16) | vendor_id

    def pci_match(gpu: ET.Element) -> bool:
        try:
            return hardware_host.parse_pci_hex(
                gpu.findtext("pci/pci_device_id")
            ) == query_pci
        except ValueError:
            return False

    matching_gpus = [item for item in root.findall("gpu") if pci_match(item)]
    if len(matching_gpus) != 1:
        raise ValueError(
            f"nvidia-smi PCI identity matched {len(matching_gpus)} GPUs; expected exactly one"
        )
    gpu = matching_gpus[0]
    virtualization = _text(gpu.findtext("gpu_virtualization_mode/virtualization_mode"))
    if virtualization.casefold() != "none":
        raise ValueError(f"NVIDIA GPU virtualization mode is {virtualization!r}, expected 'None'")

    nvidia_subsystem = hardware_host.parse_pci_hex(gpu.findtext("pci/pci_sub_system_id"))
    if nvidia_subsystem != adapter["subsystem_id"]:
        raise ValueError(
            f"NVIDIA subsystem id 0x{nvidia_subsystem:08x} disagrees with DXGI "
            f"0x{adapter['subsystem_id']:08x}"
        )

    def display_matches(row: dict) -> bool:
        try:
            return (hardware_host.parse_pci_hex(row.get("VendorID")) == vendor_id
                    and hardware_host.parse_pci_hex(row.get("DeviceID")) == device_id
                    and (not row.get("SubSysID")
                         or hardware_host.parse_pci_hex(row["SubSysID"]) == adapter["subsystem_id"]))
        except ValueError:
            return False

    displays = [row for row in raw["display_devices"] if display_matches(row)]
    if not displays:
        raise ValueError("dxdiag did not expose a display device matching DXGI vendor/device/subsystem identity")
    wddm = {_text(row["DriverModel"]) for row in displays}
    if len(wddm) != 1:
        raise ValueError(f"matching dxdiag rows disagree on WDDM driver model: {sorted(wddm)}")
    driver_model = next(iter(wddm))
    if not re.fullmatch(r"WDDM \d+\.\d+", driver_model):
        raise ValueError(f"dxdiag driver model {driver_model!r} is not a WDDM version")

    enforced_limit_w = _gpu_ceiling_power_limit(gpu)
    clocks = {}
    for kind in ("graphics_clock", "mem_clock"):
        clocks[kind] = {
            "observed_mhz": _observed_number(gpu.findtext(f"clocks/{kind}"), "MHz"),
            "maximum_mhz": _observed_number(gpu.findtext(f"max_clocks/{kind}"), "MHz"),
        }

    modes = raw["nvidia_power_profiles"]
    if set(modes) != set(MEASURED_APPLICATIONS):
        missing = sorted(set(MEASURED_APPLICATIONS) - set(modes))
        extra = sorted(set(modes) - set(MEASURED_APPLICATIONS))
        raise ValueError(f"NVIDIA power-profile coverage mismatch; missing={missing}, extra={extra}")
    for name, mode in modes.items():
        if type(mode["value"]) is not int or mode["value"] not in range(6):
            raise ValueError(f"NVIDIA power-profile value for {name} is invalid: {mode.get('value')!r}")
        if type(mode["location"]) is not int or mode["location"] not in range(4):
            raise ValueError(f"NVIDIA power-profile location for {name} is invalid: {mode.get('location')!r}")

    return {
        "execution_environment": {
            "kind": "physical",
            "hypervisor_present": hypervisor_present,
            "hypervisor_explained_by_vbs": host_evaluation[
                "hypervisor_explained_by_vbs"
            ],
            "gpu_virtualization_mode": virtualization,
            "documentation": (
                "Shared physical-host probe accepts the active hypervisor because VBS "
                "independently explains it; NVIDIA reports GPU virtualization mode None."
                if hypervisor_present else ""
            ),
        },
        "host": {
            "manufacturer": manufacturer,
            "model": model,
            "cpu": processors,
            "motherboard": board,
            "memory_bytes": memory,
            "logical_processors": logical,
        },
        "environment": {
            "wddm_driver_version": driver_model,
            "windows_power_plan": plan[0].lower(),
            "nvidia_power_management_mode": modes,
            "gpu_power_state": {
                "pstate": _text(gpu.findtext("performance_state")),
                "enforced_limit_w": enforced_limit_w,
            },
            "pcie_power_management": {"ac": pcie[-2].lower(), "dc": pcie[-1].lower()},
            "gpu_clocks": clocks,
            "temperature_c": _observed_number(gpu.findtext("temperature/gpu_temp"), "C"),
            "nvidia_driver_reported_version": _text(root.findtext("driver_version")),
            "gpu_uuid": _text(gpu.findtext("uuid")),
            "gpu_pci_bus": _text(gpu.findtext("pci/pci_bus_id")),
        },
    }


def qualify_with_diagnostics(
    raw: Mapping[str, Any], adapter: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, list[str]]:
    """Return provenance plus an exact fail-closed rejection reason."""
    try:
        return _qualify_impl(raw, adapter), []
    except (AttributeError, ValueError, TypeError, KeyError, ET.ParseError) as exc:
        return None, [f"{type(exc).__name__}: {exc}"]


def qualify(raw: Mapping[str, Any], adapter: Mapping[str, Any]) -> dict[str, Any] | None:
    """Compatibility wrapper used by evidence reconstruction."""
    provenance, _ = qualify_with_diagnostics(raw, adapter)
    return provenance
