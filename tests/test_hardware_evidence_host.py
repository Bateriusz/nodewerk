from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import hardware_evidence_host as host  # noqa: E402


def _probe(*, hypervisor: bool = False, vbs: int = 0) -> dict:
    return {
        "available": True,
        "manufacturer": "Micro-Star International Co., Ltd.",
        "model": "MS-7C56",
        "product_vendor": "Micro-Star International Co., Ltd.",
        "product_name": "MS-7C56",
        "bios_manufacturer": "American Megatrends International, LLC.",
        "bios_version": "1.0",
        "baseboard_manufacturer": "Micro-Star International Co., Ltd.",
        "baseboard_product": "B550-A PRO (MS-7C56)",
        "hypervisor_present": hypervisor,
        "virtualization_based_security_status": vbs,
        "error": "",
    }


def test_physical_vbs_host_is_not_misclassified_as_vm() -> None:
    result = host.evaluate_physical_virtualization_probe(
        _probe(hypervisor=True, vbs=2)
    )
    assert result["valid"] is True
    assert result["hypervisor_explained_by_vbs"] is True
    assert result["virtualization_indicators"] == []


def test_active_hypervisor_without_vbs_explanation_fails_closed() -> None:
    result = host.evaluate_physical_virtualization_probe(
        _probe(hypervisor=True, vbs=0)
    )
    assert result["valid"] is False
    assert "active hypervisor is not independently explained by VBS" in result["reasons"]


def test_vm_identity_is_rejected_even_when_vbs_is_active() -> None:
    probe = _probe(hypervisor=True, vbs=2)
    probe["manufacturer"] = "VMware, Inc."
    result = host.evaluate_physical_virtualization_probe(probe)
    assert result["valid"] is False
    assert "vmware" in result["virtualization_indicators"]


def test_generic_physical_identity_fails_closed() -> None:
    probe = _probe()
    probe["product_name"] = "System Product Name"
    result = host.evaluate_physical_virtualization_probe(probe)
    assert result["valid"] is False
    assert result["generic_identity"] is True


def test_pci_hex_accepts_nvidia_smi_and_dxdiag_formats() -> None:
    assert host.parse_pci_hex("280310DE") == 0x280310DE
    assert host.parse_pci_hex("0x280310DE") == 0x280310DE


def test_windows_output_decode_uses_oem_before_ansi_when_utf8_fails(monkeypatch) -> None:
    monkeypatch.setattr(host, "_windows_code_pages", lambda: ("cp852", "cp1250"))
    assert host.decode_windows_output(bytes([0x98])) == "ś"
