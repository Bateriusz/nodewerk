from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw10_pe_ledger as ledger  # noqa: E402
from schema_validator import validate_instance  # noqa: E402


def _pe(name: str, total: int, imports: list[str]) -> dict:
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
    }


def test_pe_ledger_tool_emits_schema_valid_flattened_report(
    tmp_path: Path, monkeypatch
) -> None:
    final = _pe("final", 240_702, ["kernel32.dll", "d3d12.dll"])
    baseline = _pe("baseline", 967_230, ["kernel32.dll", "d3d12.dll"])
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
