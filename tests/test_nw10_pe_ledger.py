from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw10_pe_ledger as ledger  # noqa: E402
import nw04_fixture  # noqa: E402
import nw05_export  # noqa: E402
import nw10_compact as compact  # noqa: E402
import nwp_package as nwp  # noqa: E402
from schema_validator import validate_instance  # noqa: E402


def _pe(name: str, total: int, imports: list[str]) -> dict:
    dll_characteristics = 0x0020 | 0x0040 | 0x0100 | 0x4000
    return {
        "file": name,
        "sha256": ("1" if name == "final" else "2") * 64,
        "total_bytes": total,
        "headers_bytes": 512,
        "pe_image_file_bytes": total - 128,
        "overlay_bytes": 128,
        "section_raw_bytes": total - 640,
        "sections": [
            {
                "name": ".text",
                "raw_size_bytes": total - 640,
                "virtual_size_bytes": total - 640,
                "raw_ptr": 512,
            }
        ],
        "imports": imports,
        "delay_imports": [],
        "dll_characteristics": dll_characteristics,
        "mitigations": ledger._mitigation_summary(dll_characteristics),
        "embedded": {
            "present": True,
            "payload_kind": "compact_container",
            "payload_bytes": 128,
            "payload_sha256": "b" * 64,
            "decoded_package_bytes": 96,
            "decoded_package_sha256": "a" * 64,
            "embedded_manifest_sha256": "a" * 64,
            "codec_id": "nwc.codec.mszip.v1",
            "layout_id": "nwc.layout.soa.v1",
            "decode_error": None,
        },
    }


def test_pe_ledger_tool_emits_schema_valid_flattened_report(
    tmp_path: Path, monkeypatch
) -> None:
    final = _pe(
        "final", 240_702, ["kernel32.dll", "d3d12.dll", "cabinet.dll"]
    )
    baseline = _pe(
        "baseline", 967_230, ["kernel32.dll", "d3d12.dll", "cabinet.dll"]
    )
    decoder_off = _pe("decoder_off", 506_880, ["kernel32.dll"])
    decoder_on = _pe("decoder_on", 510_464, ["cabinet.dll", "kernel32.dll"])

    def fake_pe_summary(path: Path) -> dict:
        return {
            "final": final,
            "baseline": baseline,
            "decoder_off": decoder_off,
            "decoder_on": decoder_on,
        }[path.stem]

    monkeypatch.setattr(ledger, "pe_summary", fake_pe_summary)
    monkeypatch.setattr(
        ledger,
        "sha256_file",
        lambda path: "a" * 64,
    )
    for name in ("final.nwp", "baseline.nwp"):
        (tmp_path / name).write_bytes(b"fixture")
    (tmp_path / "final.json").write_text(
        json.dumps({"profile_id": "nw.common.d3d12.fl12_0.sm6_0.v1"}),
        encoding="utf-8",
    )
    report = ledger.build_report(
        final_executable=tmp_path / "final.exe",
        linker_baseline_executable=tmp_path / "baseline.exe",
        decoder_on_template=tmp_path / "decoder_on.exe",
        decoder_off_template=tmp_path / "decoder_off.exe",
        final_package=tmp_path / "final.nwp",
        linker_baseline_package=tmp_path / "baseline.nwp",
        final_manifest=tmp_path / "final.json",
        linker_baseline_manifest=tmp_path / "final.json",
    )
    schema = json.loads(
        (REPO_ROOT / "schemas" / "nw10-windows-pe-ledger.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validate_instance(report, schema, schema)
    assert report["final_artifact"]["pe_image_file_bytes"] == 240_574
    assert report["final_artifact"]["overlay_bytes"] == 128
    assert report["linker_elimination"]["control_executable_bytes"] == 967_230
    assert report["compact_decoder"]["decoder_on_template_bytes"] == 510_464
    assert report["compact_decoder"]["decoder_off_template_bytes"] == 506_880
    assert report["compact_decoder"]["runtime_import_added"] == "cabinet.dll"
    assert report["conclusion"]["byte_ledger_complete"] is True
    assert report["final_artifact"]["package_binding_verified"] is True
    assert report["final_artifact"]["embedded_payload_kind"] == "compact_container"
    assert report["linker_elimination"]["security_mitigations_preserved"] is True
    assert report["linker_elimination"]["canonical_package_identical"] is True
    assert report["linker_elimination"]["compact_decoder_preserved"] is True


def test_embedded_payload_summary_binds_canonical_and_compact_payloads() -> None:
    package = nwp.encode_package(
        nwp.make_test_package(shader_blob=nw04_fixture.make_dxil_container())
    )
    manifest_digest = b"\x11" * 32
    canonical_executable = nw05_export.embed_package(
        b"MZ" + b"\x00" * 128, package, manifest_digest
    )
    canonical = ledger._embedded_payload_summary(canonical_executable)
    assert canonical["payload_kind"] == "canonical_package"
    assert canonical["decoded_package_sha256"] == hashlib.sha256(package).hexdigest()
    assert canonical["embedded_manifest_sha256"] == manifest_digest.hex()

    encoded = compact.encode_compact(
        package,
        codec_id=compact.contract.CODEC_MSZIP_ID,
        layout_id=compact.contract.LAYOUT_SOA_ID,
    )
    compact_executable = nw05_export.embed_package(
        b"MZ" + b"\x00" * 128, encoded.bytes, manifest_digest
    )
    summary = ledger._embedded_payload_summary(compact_executable)
    assert summary["payload_kind"] == "compact_container"
    assert summary["decoded_package_sha256"] == hashlib.sha256(package).hexdigest()
    assert summary["codec_id"] == compact.contract.CODEC_MSZIP_ID
    assert summary["layout_id"] == compact.contract.LAYOUT_SOA_ID


def test_build_tree_ltgc_evidence_reads_ninja_flags(tmp_path: Path) -> None:
    build_root = tmp_path / "build"
    build_root.mkdir()
    (build_root / "CMakeCache.txt").write_text("", encoding="utf-8")
    (build_root / "build.ninja").write_text(
        "FLAGS = /GL /O1\nLINK_FLAGS = /LTCG /OPT:REF\n", encoding="utf-8"
    )
    template = build_root / "staging" / "player.exe"
    template.parent.mkdir()
    template.write_bytes(b"MZ")
    evidence = ledger._build_tree_ltgc_evidence(template)
    assert evidence["status"] == "verified_build_tree_flags"
    assert evidence["has_gl"] is True
    assert evidence["has_ltcg"] is True
    assert evidence["ltgc_preserved"] is True


def test_removed_security_mitigation_fails_closed(tmp_path: Path, monkeypatch) -> None:
    baseline = _pe("baseline", 967_230, ["kernel32.dll", "d3d12.dll", "cabinet.dll"])
    final = _pe(
        "final", 240_702, ["kernel32.dll", "d3d12.dll", "cabinet.dll"]
    )
    final["dll_characteristics"] = 0x0020 | 0x0040 | 0x0100
    decoder_off = _pe("decoder_off", 506_880, ["kernel32.dll"])
    decoder_on = _pe("decoder_on", 510_464, ["cabinet.dll", "kernel32.dll"])

    monkeypatch.setattr(
        ledger,
        "pe_summary",
        lambda path: {
            "final": final,
            "baseline": baseline,
            "decoder_off": decoder_off,
            "decoder_on": decoder_on,
        }[path.stem],
    )
    monkeypatch.setattr(ledger, "sha256_file", lambda path: "a" * 64)
    for name in ("final.nwp", "baseline.nwp"):
        (tmp_path / name).write_bytes(b"fixture")
    manifest = tmp_path / "final.json"
    manifest.write_text(
        json.dumps({"profile_id": "nw.common.d3d12.fl12_0.sm6_0.v1"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="security mitigation"):
        ledger.build_report(
            final_executable=tmp_path / "final.exe",
            linker_baseline_executable=tmp_path / "baseline.exe",
            decoder_on_template=tmp_path / "decoder_on.exe",
            decoder_off_template=tmp_path / "decoder_off.exe",
            final_package=tmp_path / "final.nwp",
            linker_baseline_package=tmp_path / "baseline.nwp",
            final_manifest=manifest,
            linker_baseline_manifest=manifest,
        )
