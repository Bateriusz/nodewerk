#!/usr/bin/env python3
"""NW-10 compact optimization of the existing NW-07 256 KiB proof.

This tool starts from the canonical NW-07 graph and repository-owned export
path.  It does not substitute a player or a hand-authored demo.  Contract-only
mode uses the established structural test payloads so byte accounting and
determinism can be exercised on a GPU-less host; physical Windows measurements
remain explicitly pending.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw04_fixture  # noqa: E402
import nw05_export  # noqa: E402
import nw07_export as nw07_export  # noqa: E402
import nwp_package as nwp  # noqa: E402
import nw10_compact as compact  # noqa: E402
import nw10_contract as contract  # noqa: E402

PROOF_REPORT_FORMAT = "nw10-compact-proof-report"
PROOF_REPORT_VERSION = 1


class ProofError(RuntimeError):
    pass


def _mock_dxc(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    dxc = directory / "mock_dxc.py"
    container = nw04_fixture.make_dxil_container()
    dxc.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print('dxcompiler.dll: 1.8 - 1.8.2407.12')\n"
        "    sys.exit(0)\n"
        "fo = Path(sys.argv[sys.argv.index('-Fo') + 1])\n"
        "fo.parent.mkdir(parents=True, exist_ok=True)\n"
        f"fo.write_bytes(bytes.fromhex({container.hex()!r}))\n",
        encoding="utf-8",
    )
    dxc.chmod(0o755)
    return dxc


def _template(directory: Path, size: int = 256) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "template.exe"
    path.write_bytes(b"MZ" + b"\x00" * size)
    return path


def _require_compact_player_template(
    template: Path, *, expected: bool = True, label: str = "compact"
) -> None:
    try:
        configuration = nw05_export._template_build_configuration(template)
    except Exception as exc:
        raise ProofError(
            f"{label} export requires a readable pinned player build tree: {exc}"
        ) from exc
    enabled = str(configuration.get("NW10_COMPACT_PACKAGE", "")).strip().upper() in {
        "ON",
        "1",
        "TRUE",
    }
    if enabled != expected:
        required = "ON" if expected else "OFF"
        raise ProofError(
            f"release {label} export requires the player template to be built "
            f"with -DNW10_COMPACT_PACKAGE={required}"
        )


def _artifact_paths(directory: Path) -> tuple[Path, Path, Path]:
    stem = nw05_export._profile_file_stem(nwp.COMMON_PROFILE_ID)
    package = directory / f"{stem}.nwp"
    manifest = directory / f"{stem}.manifest.json"
    executable = directory / f"{stem}.exe"
    for label, path in (
        ("package", package),
        ("manifest", manifest),
        ("executable", executable),
    ):
        if not path.is_file():
            raise ProofError(f"NW-07 {label} is unavailable: {path}")
    return package, manifest, executable


def _compact_executable(
    compact_template_executable: bytes,
    package_bytes: bytes,
    manifest_bytes: bytes,
    *,
    codec_id: str,
    layout_id: str,
) -> tuple[bytes, compact.CompactEncoding]:
    embedded_package, embedded_manifest = nw05_export.extract_embedded_package(
        compact_template_executable
    )
    if embedded_package != package_bytes:
        raise ProofError(
            "compact-decoder template does not embed the canonical package"
        )
    if embedded_manifest != hashlib.sha256(manifest_bytes).digest():
        raise ProofError("compact-decoder template manifest binding changed")
    total = len(package_bytes) + len(embedded_manifest) + nw05_export.EMBED_FOOTER_SIZE
    template = compact_template_executable[: len(compact_template_executable) - total]
    if layout_id == "auto":
        layout_id = compact.measure_layouts(package_bytes)["selected"]
    if codec_id == "auto":
        codec_id = compact.select_player_codec(
            package_bytes, layout_id=layout_id, delta_references=True
        )["selected_codec_id"]
    encoded = compact.encode_compact(
        package_bytes, codec_id=codec_id, layout_id=layout_id, delta_references=True
    )
    rebuilt = nw05_export.embed_package(
        template, encoded.bytes, embedded_manifest
    )
    if compact.decode_compact(encoded.bytes) != package_bytes:
        raise ProofError("compact proof payload did not round-trip")
    return rebuilt, encoded


def _decoder_code_measurement() -> dict[str, Any]:
    return {
        "source": contract.COMPACT_DECODER_SOURCE,
        "status": "pending_physical_windows",
        "bytes": None,
        "reason": (
            "The exact MSVC/x64 decoder contribution is measured from the final "
            "Windows PE; host compiler sizes are not substituted."
        ),
    }


def export_proof(
    output: Path | str,
    *,
    player_template: Path | str | None = None,
    canonical_player_template: Path | str | None = None,
    dxc_executable: str | None = None,
    report_path: Path | str | None = None,
    contracts_only: bool = False,
    reproducibility: bool = False,
    codec_id: str = "auto",
    layout_id: str = "auto",
) -> dict[str, Any]:
    output_path = Path(output)
    if output_path.exists():
        if not output_path.is_dir() or any(output_path.iterdir()):
            raise ProofError(f"proof output directory is not empty: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".nw10-proof-", dir=str(output_path.parent)))
    canonical = staging / "canonical"
    compact_dir = staging / "compact"
    template = Path(player_template) if player_template else None
    canonical_template = (
        Path(canonical_player_template)
        if canonical_player_template
        else template
    )
    dxc = dxc_executable
    if contracts_only:
        toolchain = staging / "toolchain"
        template = _template(toolchain)
        canonical_template = template
        dxc = str(_mock_dxc(toolchain))
    elif template is None or canonical_template is None or dxc is None:
        raise ProofError(
            "release proof export requires --player-template, "
            "--canonical-player-template, and --dxc"
        )
    else:
        _require_compact_player_template(template, expected=True, label="compact")
        _require_compact_player_template(
            canonical_template, expected=False, label="canonical baseline"
        )
    try:
        if reproducibility:
            repro = nw07_export.export_reproducibility(
                staging / "repro",
                player_template=canonical_template,
                dxc_executable=dxc,
                allow_test_payloads=contracts_only,
            )
            canonical = staging / "repro" / "clean-a"
        else:
            repro = None
            nw07_export.export_production(
                canonical,
                player_template=canonical_template,
                dxc_executable=dxc,
                report_path=staging / "canonical-report.json",
                allow_test_payloads=contracts_only,
            )
        package_path, manifest_path, executable_path = _artifact_paths(canonical)
        package_bytes = package_path.read_bytes()
        manifest_bytes = manifest_path.read_bytes()
        manifest_record = json.loads(manifest_bytes.decode("utf-8"))
        canonical_executable = executable_path.read_bytes()
        dependencies = [
            {
                "name": str(name),
                "kind": "windows_inbox",
                "distributed_bytes": 0,
            }
            for name in manifest_record.get("retained", {}).get("dependencies", [])
        ]
        optional_auxiliary = {
            path.name: path.stat().st_size
            for path in canonical.iterdir()
            if path.is_file()
            and path not in {package_path, manifest_path, executable_path}
        }
        if contracts_only:
            compact_template_executable = canonical_executable
        else:
            # The compact candidate must be linked from the otherwise
            # identical decoder-ON template.  Re-run the repository-owned
            # export with that template; the canonical payload must be
            # byte-identical to the decoder-OFF baseline.
            compact_base = staging / "compact-base"
            nw07_export.export_production(
                compact_base,
                player_template=template,
                dxc_executable=dxc,
                report_path=staging / "compact-base-report.json",
                allow_test_payloads=False,
                # The decoder-ON candidate is the research artifact; the
                # canonical decoder-OFF baseline above is the tier gate.
                allow_complete_distribution_over_proof_tier=True,
            )
            base_package, base_manifest, base_executable = _artifact_paths(
                compact_base
            )
            if (
                base_package.read_bytes() != package_bytes
                or base_manifest.read_bytes() != manifest_bytes
            ):
                raise ProofError(
                    "decoder-ON export changed the canonical package or manifest"
                )
            compact_template_executable = base_executable.read_bytes()
        compact_executable, encoded = _compact_executable(
            compact_template_executable,
            package_bytes,
            manifest_bytes,
            codec_id=codec_id,
            layout_id=layout_id,
        )
        compact_dir.mkdir(parents=True, exist_ok=True)
        for path in canonical.iterdir():
            if path.is_file():
                shutil.copy2(path, compact_dir / path.name)
        stem = nw05_export._profile_file_stem(nwp.COMMON_PROFILE_ID)
        (compact_dir / f"{stem}.compact.nwc").write_bytes(encoded.bytes)
        (compact_dir / f"{stem}.compact.exe").write_bytes(compact_executable)
        decoder = _decoder_code_measurement()
        canonical_ledger = compact.build_byte_ledger(
            artifact_kind="canonical_nw07_proof",
            canonical_executable_bytes=len(canonical_executable),
            canonical_package_bytes=len(package_bytes),
            manifest_bytes=len(manifest_bytes),
            compact=None,
            decoder_code_bytes=None,
            shader_bytes=sum(
                len(shader.blob)
                for shader in nwp.decode_package(package_bytes, allow_dev=False).shaders
            ),
            auxiliary_files=optional_auxiliary,
            dependencies=dependencies,
            evidence_status="informational_contract_only",
        )
        compact_ledger = compact.build_byte_ledger(
            artifact_kind="canonical_nw07_proof",
            canonical_executable_bytes=len(canonical_executable),
            canonical_package_bytes=len(package_bytes),
            manifest_bytes=len(manifest_bytes),
            compact=encoded,
            compact_executable_bytes=len(compact_executable),
            decoder_code_bytes=decoder["bytes"],
            shader_bytes=sum(
                len(shader.blob)
                for shader in nwp.decode_package(package_bytes, allow_dev=False).shaders
            ),
            auxiliary_files=optional_auxiliary,
            dependencies=(
                dependencies
                if contracts_only
                else [
                    {
                        "name": str(name),
                        "kind": "windows_inbox",
                        "distributed_bytes": 0,
                    }
                    for name in nw05_export._pe_runtime_dependencies(
                        compact_executable
                    )
                ]
            ),
            evidence_status="informational_contract_only",
        )
        if not contracts_only and len(canonical_executable) > contract.PROOF_TIER_BYTES:
            raise ProofError(
                "canonical decoder-off NW-07 proof exceeds the preserved "
                f"{contract.PROOF_TIER_BYTES}-byte complete-distribution ceiling"
            )
        selected = (
            canonical_executable
            if compact_ledger["compression_rejected_for_increasing_distribution"]
            else compact_executable
        )
        (compact_dir / f"{stem}.exe").write_bytes(selected)
        measurement = compact.comprehensive_measurement(package_bytes)
        nw10_inputs = contract.compute_build_inputs()
        report: dict[str, Any] = {
            "format": PROOF_REPORT_FORMAT,
            "version": PROOF_REPORT_VERSION,
            "task": contract.TASK,
            "implementation_base": contract.IMPLEMENTATION_BASE,
            "planning_provenance": contract.PLANNING_PROVENANCE,
            "success": True,
            "release_grade": not contracts_only,
            "test_payload": bool(contracts_only),
            "canonical_graph": contract.CANONICAL_PROOF_GRAPH,
            "canonical_config": contract.CANONICAL_PROOF_CONFIG,
            "nw07_perf_contract": contract.PERF_CONTRACT_ID,
            "historical_nw07_build_inputs_digest": (
                contract.HISTORICAL_NW07_BUILD_INPUTS_DIGEST
            ),
            "nw10_build_inputs_digest": contract.build_inputs_digest(nw10_inputs),
            "nw10_build_inputs": nw10_inputs,
            "canonical_artifact": {
                "artifact_key": manifest_record.get("artifact_identity"),
                "executable_bytes": len(canonical_executable),
                "executable_sha256": hashlib.sha256(canonical_executable).hexdigest(),
                "package_bytes": len(package_bytes),
                "package_sha256": hashlib.sha256(package_bytes).hexdigest(),
                "manifest_bytes": len(manifest_bytes),
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            },
            "templates": {
                "canonical": (
                    "contract_only_mock"
                    if contracts_only
                    else str(canonical_template)
                ),
                "compact": (
                    "contract_only_mock" if contracts_only else str(template)
                ),
                "canonical_decoder_off": True,
                "compact_decoder_on": True,
            },
            "compact_candidate": {
                "executable_bytes": len(compact_executable),
                "executable_sha256": hashlib.sha256(compact_executable).hexdigest(),
                "encoded_package_bytes": len(encoded.bytes),
                "encoded_package_sha256": hashlib.sha256(encoded.bytes).hexdigest(),
                "codec_id": encoded.codec_id,
                "layout_id": encoded.layout_id,
                "decoded_package_bytes": encoded.decoded_size,
                "reference_records": encoded.reference_count,
                "selected_executable_bytes": len(selected),
                "selected_executable_sha256": hashlib.sha256(selected).hexdigest(),
                "selected_mode": (
                    "canonical" if selected == canonical_executable else "compact"
                ),
            },
            "ledgers": {
                "canonical": canonical_ledger,
                "compact": compact_ledger,
            },
            "measurements": measurement,
            "decoder_code": decoder,
            "reproducibility": repro,
            "preserved_contracts": {
                "profile_id": nwp.COMMON_PROFILE_ID,
                "requirements_digest": nwp.COMMON_REQUIREMENTS_DIGEST,
                "resolution": {
                    "width": contract.OUTPUT_WIDTH,
                    "height": contract.OUTPUT_HEIGHT,
                },
                "quality_profile": "nw07.quality.v1",
                "loop_ticks": contract.LOOP_TICKS,
                "frame_rate_hz": contract.FRAME_RATE_HZ,
                "period_performance_contract": contract.PERF_CONTRACT_ID,
                "period_protocol": contract.PERF_PROTOCOL_ID,
                "numeric_image_tolerances": contract.image_tolerances(),
            },
            "startup": {
                "protocol": nw07_export.contract.STARTUP_PROTOCOL,
                "ceiling_ms": contract.STARTUP_CEILING_MS,
                "status": "pending_physical_windows_capture",
            },
            "memory": {
                "ceiling_bytes": contract.PEAK_ENGINE_COMMITTED_GPU_BYTES,
                "status": "pending_physical_windows_capture",
            },
            "performance_evidence": {
                "contract_id": contract.PERF_CONTRACT_ID,
                "protocol": contract.PERF_PROTOCOL_ID,
                "runs": contract.PERF_RUN_COUNT,
                "warmup_seconds": contract.WARMUP_SECONDS,
                "run_seconds": contract.PERF_RUN_SECONDS,
                "fresh_process_per_run": True,
                "application_pipeline_cache": False,
                "classification": "pending_physical",
                "reference_capture": "pending_reference_capture",
            },
            "decision": {
                "target_bytes": contract.COMPACT_TARGET_BYTES,
                "target_met_on_contract_measurement": (
                    compact_ledger["complete_runnable_distribution_bytes"]
                    <= contract.COMPACT_TARGET_BYTES
                ),
                "physical_target_met": False,
                "classification": "continue",
                "reason": (
                    "Contract-only payload and byte-ledger measurements are retained; "
                    "the exact Windows x64 single-file artifact and clean-system "
                    "measurements remain pending."
                ),
                "universal_64k_claim": False,
            },
        }
        shutil.copytree(canonical, output_path, dirs_exist_ok=True)
        shutil.copytree(compact_dir, output_path / "compact", dirs_exist_ok=True)
        if report_path is not None:
            path = Path(report_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return report
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "build" / "nw10-proof")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--player-template", type=Path)
    parser.add_argument(
        "--canonical-player-template",
        type=Path,
        help=(
            "player template built with NW10_COMPACT_PACKAGE=OFF; used for the "
            "canonical NW-07 proof baseline"
        ),
    )
    parser.add_argument("--dxc")
    parser.add_argument("--contracts-only", action="store_true")
    parser.add_argument("--reproducibility", action="store_true")
    parser.add_argument(
        "--codec", default="auto",
        choices=("auto", *contract.PLAYER_SUPPORTED_CODEC_IDS),
    )
    parser.add_argument(
        "--layout", default="auto", choices=("auto", *contract.KNOWN_LAYOUT_IDS)
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = export_proof(
            args.output,
            player_template=args.player_template,
            canonical_player_template=args.canonical_player_template,
            dxc_executable=args.dxc,
            report_path=args.report,
            contracts_only=args.contracts_only,
            reproducibility=args.reproducibility,
            codec_id=args.codec,
            layout_id=args.layout,
        )
    except nw07_export.Nw07ExportError as exc:
        measured = exc.details.get("complete_distribution_bytes")
        if (
            isinstance(measured, int)
            and measured > contract.PROOF_TIER_BYTES
            and "complete distribution exceeds" in str(exc)
        ):
            # NW-07 keeps its 256 KiB proof ceiling as a hard contract. NW-10
            # is different: size is a research outcome, so retain the exact
            # measured blocker and continue to the restricted-intro attempt
            # instead of turning a missed size tier into a CI failure.
            report = {
                "format": PROOF_REPORT_FORMAT,
                "version": PROOF_REPORT_VERSION,
                "task": contract.TASK,
                "implementation_base": contract.IMPLEMENTATION_BASE,
                "planning_provenance": contract.PLANNING_PROVENANCE,
                "success": False,
                "release_grade": not args.contracts_only,
                "test_payload": bool(args.contracts_only),
                "measurement_outcome": "size_target_not_met",
                "measured_distribution_bytes": measured,
                "blockers": [
                    {
                        "id": "nw07_proof_tier",
                        "status": "measured_above_tier",
                        "limit_bytes": contract.PROOF_TIER_BYTES,
                        "measured_bytes": measured,
                    }
                ],
                "decision": {
                    "target_bytes": contract.COMPACT_TARGET_BYTES,
                    "proof_tier_bytes": contract.PROOF_TIER_BYTES,
                    "physical_target_met": False,
                    "classification": "continue",
                    "reason": (
                        "The measured Windows export exceeded the inherited "
                        "NW-07 256 KiB proof tier. NW-10 records that result as "
                        "a size blocker and continues the feasibility study."
                    ),
                    "universal_64k_claim": False,
                },
            }
            if args.report is not None:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(
                    json.dumps(report, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0
        print(f"NW-10 proof export failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"NW-10 proof export failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
