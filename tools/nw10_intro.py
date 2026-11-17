#!/usr/bin/env python3
"""NW-10 restricted SDF intro export and compact-proof comparison.

The intro remains inside the established pipeline:

    .nwgraph -> NW-03 compiler -> .nwp v1 package -> NW-05 export/player

Only the offline shader specialization is intro-specific.  The graph keeps the
unchanged procedural texture and fullscreen/present operators, while the frozen
shader implements the sphere/torus SDF composition, seeded material sampling,
absolute-tick camera curves, and ACES tonemapping.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw03_compiler as nw03  # noqa: E402
import nw04_fixture  # noqa: E402
import nw05_export  # noqa: E402
import nw05_optimizer as optimizer  # noqa: E402
import nw07_export as nw07_export  # noqa: E402
import nwp_package as nwp  # noqa: E402
import nw10_compact as compact  # noqa: E402
import nw10_contract as contract  # noqa: E402

INTRO_GRAPH = REPO_ROOT / contract.INTRO_GRAPH
INTRO_SHADER = REPO_ROOT / contract.INTRO_SHADER
INTRO_REPORT_FORMAT = "nw10-intro-report"
INTRO_REPORT_VERSION = 1


class IntroContractError(RuntimeError):
    """Raised when the restricted intro leaves its frozen profile."""


def _setting(document: nw03.GraphDocument, name: str) -> str:
    return str(document.settings.get(name, "")).strip()


def _float_setting(document: nw03.GraphDocument, name: str) -> float:
    try:
        value = float(_setting(document, name))
    except ValueError as exc:
        raise IntroContractError(f"setting '{name}' is not numeric") from exc
    if not math.isfinite(value):
        raise IntroContractError(f"setting '{name}' is not finite")
    return value


def _operator_set(document: nw03.GraphDocument) -> tuple[str, ...]:
    optimized = optimizer.optimize_graph(document)
    return tuple(sorted({node.operator_id for node in optimized.nodes}))


def validate_intro_graph(path: Path | str = INTRO_GRAPH) -> dict[str, Any]:
    graph_path = Path(path)
    if not graph_path.is_file():
        raise IntroContractError(f"intro graph is unavailable: {graph_path}")
    if not INTRO_SHADER.is_file():
        raise IntroContractError(f"intro shader is unavailable: {INTRO_SHADER}")
    document = nw03.load_graph(graph_path)
    if document.profile_id != nwp.COMMON_PROFILE_ID:
        raise IntroContractError("intro changed the common capability profile")
    if document.requirements_digest != nwp.COMMON_REQUIREMENTS_DIGEST:
        raise IntroContractError("intro changed the common requirements digest")
    if _setting(document, "nw10_intro_profile") != contract.INTRO_PROFILE_ID:
        raise IntroContractError("intro profile identifier changed")
    if _setting(document, "nw10_intro_loop_ticks") != str(contract.LOOP_TICKS):
        raise IntroContractError("intro loop is not 960,000 ticks")
    if _float_setting(document, "nw10_intro_loop_seconds") != contract.LOOP_SECONDS:
        raise IntroContractError("intro duration is not 20 seconds")
    if (
        _setting(document, "nw07_output_width") != str(contract.OUTPUT_WIDTH)
        or _setting(document, "nw07_output_height") != str(contract.OUTPUT_HEIGHT)
    ):
        raise IntroContractError("intro presentation is not 1920x1080")

    operators = _operator_set(document)
    if operators != tuple(sorted(contract.INTRO_OPERATORS)):
        raise IntroContractError(
            f"intro operator set changed: {operators!r}"
        )
    source = INTRO_SHADER.read_text(encoding="utf-8")
    required_tokens = (
        "sdSphere",
        "sdTorus",
        "mapScene",
        "acesTonemap",
        "sampleMaterial",
        "SDFIntroCS",
        "NW_LOOP_TICKS",
    )
    missing = [token for token in required_tokens if token not in source]
    if missing:
        raise IntroContractError(f"intro shader is missing {missing}")
    forbidden = {
        "audio": ("audio", "sound", "waveout", "xaudio"),
        "mesh_pipeline": ("mesh", "torusinstance", "vertexbuffer", "indexbuffer"),
        "hardware_rt": ("raytracing", "raytracingtier", "dispatchrays", "dxil library"),
        "runtime_dxc": ("d3dcompiler", "dxcompiler", "loadlibrary"),
        "external_assets": ("fopen", "fileopen", "content/", "assets/"),
    }
    lowered = source.lower()
    violations: dict[str, list[str]] = {}
    for category, tokens in forbidden.items():
        hits = [token for token in tokens if token in lowered]
        if hits:
            violations[category] = hits
    if violations:
        raise IntroContractError(f"intro shader leaves the restricted profile: {violations}")

    optimized = optimizer.optimize_graph(document)
    if "initialize" in optimized.partitions:
        raise IntroContractError("intro must not declare a stateful initialization partition")
    width, height = nw05_export._graph_render_dimensions(document, optimized)
    if (width, height) != (contract.OUTPUT_WIDTH, contract.OUTPUT_HEIGHT):
        raise IntroContractError("intro render dimensions changed")
    record = {
        "profile_id": contract.INTRO_PROFILE_ID,
        "graph": contract.INTRO_GRAPH,
        "graph_sha256": hashlib.sha256(graph_path.read_bytes()).hexdigest(),
        "shader": contract.INTRO_SHADER,
        "shader_sha256": hashlib.sha256(INTRO_SHADER.read_bytes()).hexdigest(),
        "loop_ticks": contract.LOOP_TICKS,
        "loop_seconds": contract.LOOP_SECONDS,
        "ticks_per_second": contract.TICKS_PER_SECOND,
        "frame_rate_hz": contract.FRAME_RATE_HZ,
        "width": width,
        "height": height,
        "operators": list(operators),
        "features": list(contract.INTRO_REQUIRED_FEATURES),
        "audio": False,
        "mesh_pipeline": False,
        "hardware_ray_tracing_required": False,
        "runtime_dxc": False,
        "external_assets": False,
        "numeric_image_tolerances": contract.image_tolerances(),
        "fixed_tick_replay": True,
        "capture_protocol": nw07_export.contract.CORRECTNESS_CAPTURE_PROTOCOL,
        "performance_contract": contract.PERF_CONTRACT_ID,
        "performance_protocol": contract.PERF_PROTOCOL_ID,
        "evidence_status": "informational_contract_only",
        "physical_measurements": "pending_physical_windows_capture",
    }
    contract.validate_intro_profile(record)
    return record


def _intro_generated_shader(document: nw03.GraphDocument) -> nw03.GeneratedShader:
    hlsl = INTRO_SHADER.read_text(encoding="utf-8")
    if "CSMain" in hlsl or "SDFIntroCS" not in hlsl:
        raise IntroContractError("intro shader entry point changed")
    return nw03.GeneratedShader(
        specialization_id="image",
        entry_point="SDFIntroCS",
        target="cs_6_0",
        family=nw03.PASS_FAMILY,
        hlsl=hlsl,
        source_map=(),
        root_expression="nw10.sdf_intro",
        root_node_ids=("material_recipe", "material_gpu", "image", "screen", "present"),
        thread_group=(1024, 1, 1),
        execution_root="image",
    )


class _IntroShaderOverride:
    def __init__(self, document: nw03.GraphDocument):
        self.document = document
        self.original = nw03.lower_to_fused_hlsl

    def __enter__(self) -> "_IntroShaderOverride":
        original = self.original
        shader = _intro_generated_shader(self.document)

        def lower(document: nw03.GraphDocument, *args: Any, **kwargs: Any):
            del document, args, kwargs
            return [shader], []

        nw03.lower_to_fused_hlsl = lower  # type: ignore[assignment]
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        nw03.lower_to_fused_hlsl = self.original  # type: ignore[assignment]


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


def _require_compact_player_template(template: Path) -> None:
    try:
        configuration = nw05_export._template_build_configuration(template)
    except Exception as exc:
        raise IntroContractError(
            f"compact export requires a readable pinned player build tree: {exc}"
        ) from exc
    if str(configuration.get("NW10_COMPACT_PACKAGE", "")).strip().upper() not in {
        "ON",
        "1",
        "TRUE",
    }:
        raise IntroContractError(
            "release compact export requires the player template to be built "
            "with -DNW10_COMPACT_PACKAGE=ON"
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
            raise IntroContractError(f"intro {label} is unavailable: {path}")
    return package, manifest, executable


def _compact_executable(
    canonical_executable: bytes,
    package_bytes: bytes,
    manifest_bytes: bytes,
    *,
    codec_id: str,
    layout_id: str,
) -> tuple[bytes, compact.CompactEncoding]:
    embedded_package, embedded_manifest = nw05_export.extract_embedded_package(
        canonical_executable
    )
    if embedded_package != package_bytes:
        raise IntroContractError("canonical executable does not embed the package")
    if embedded_manifest != hashlib.sha256(manifest_bytes).digest():
        raise IntroContractError("canonical executable manifest binding changed")
    total = len(package_bytes) + len(embedded_manifest) + nw05_export.EMBED_FOOTER_SIZE
    template = canonical_executable[: len(canonical_executable) - total]
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
    # Decode exactly as the player will before accepting the candidate.
    if compact.decode_compact(encoded.bytes) != package_bytes:
        raise IntroContractError("compact executable payload did not round-trip")
    return rebuilt, encoded


def export_intro(
    output: Path | str,
    *,
    player_template: Path | str | None = None,
    dxc_executable: str | None = None,
    report_path: Path | str | None = None,
    contracts_only: bool = False,
    codec_id: str = "auto",
    layout_id: str = "auto",
    repo_root: Path | None = None,
) -> dict[str, Any]:
    root = Path(repo_root or REPO_ROOT)
    profile = validate_intro_graph(root / contract.INTRO_GRAPH)
    document = nw03.load_graph(root / contract.INTRO_GRAPH)
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".nw10-intro-", dir=str(output_path.parent))
    )
    canonical = staging / "canonical"
    compact_dir = staging / "compact"
    template_path = Path(player_template) if player_template else None
    dxc = dxc_executable
    if contracts_only:
        toolchain = staging / "toolchain"
        dxc = str(_mock_dxc(toolchain))
        template_path = _template(toolchain)
    elif template_path is None or dxc is None:
        raise IntroContractError(
            "release intro export requires --player-template and --dxc"
        )
    else:
        _require_compact_player_template(template_path)
    settings = nw05_export.ExportSettings(
        # NW-10 size is a measured research outcome, not a hard acceptance
        # gate. Keep only a generous safety bound here; the byte ledger below
        # records 64/128/256 KiB and 1 MiB tier results explicitly.
        byte_ceiling=64 * 1024 * 1024,
        memory_budget=contract.PEAK_ENGINE_COMMITTED_GPU_BYTES,
        target_profile=nwp.COMMON_PROFILE_ID,
        requirements_digest=nwp.COMMON_REQUIREMENTS_DIGEST,
        frames_in_flight=2,
        player_template=str(template_path),
        specialized_build_root=str(staging / "native-build"),
        present_sync_interval=0,
        frozen_embedded_specialization=True,
        include_optional_artifacts=False,
        allow_optional_profiles=False,
    )
    try:
        with _IntroShaderOverride(document):
            result = nw05_export.export_graph(
                root / contract.INTRO_GRAPH,
                canonical,
                settings=settings,
                report_path=staging / "canonical-report.json",
                dxc_executable=dxc,
                optimization="O2",
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
        compact_executable, encoded = _compact_executable(
            canonical_executable,
            package_bytes,
            manifest_bytes,
            codec_id=codec_id,
            layout_id=layout_id,
        )
        compact_dir.mkdir(parents=True, exist_ok=True)
        for path in (
            package_path,
            manifest_path,
            executable_path,
            *(item for item in canonical.iterdir() if item.is_file()),
        ):
            if path.is_file():
                shutil.copy2(path, compact_dir / path.name)
        stem = nw05_export._profile_file_stem(nwp.COMMON_PROFILE_ID)
        (compact_dir / f"{stem}.compact.nwc").write_bytes(encoded.bytes)
        (compact_dir / f"{stem}.compact.exe").write_bytes(compact_executable)
        package_summary = compact.package_summary(package_bytes)
        canonical_ledger = compact.build_byte_ledger(
            artifact_kind="restricted_intro",
            canonical_executable_bytes=len(canonical_executable),
            canonical_package_bytes=len(package_bytes),
            manifest_bytes=len(manifest_bytes),
            compact=None,
            decoder_code_bytes=None,
            shader_bytes=package_summary["shader_bytes"],
            auxiliary_files=optional_auxiliary,
            dependencies=dependencies,
            evidence_status="informational_contract_only",
        )
        compact_ledger = compact.build_byte_ledger(
            artifact_kind="restricted_intro",
            canonical_executable_bytes=len(canonical_executable),
            canonical_package_bytes=len(package_bytes),
            manifest_bytes=len(manifest_bytes),
            compact=encoded,
            decoder_code_bytes=None,
            shader_bytes=package_summary["shader_bytes"],
            auxiliary_files=optional_auxiliary,
            dependencies=dependencies,
            evidence_status="informational_contract_only",
        )
        # The selected runnable executable is the smaller complete
        # distribution.  Compression that increases total distributed bytes is
        # never presented as the artifact.
        selected_executable = (
            canonical_executable
            if compact_ledger["compression_rejected_for_increasing_distribution"]
            else compact_executable
        )
        (compact_dir / f"{stem}.exe").write_bytes(selected_executable)
        selected_sha256 = hashlib.sha256(selected_executable).hexdigest()
        measurement = compact.comprehensive_measurement(package_bytes)
        report: dict[str, Any] = {
            "format": INTRO_REPORT_FORMAT,
            "version": INTRO_REPORT_VERSION,
            "task": contract.TASK,
            "implementation_base": contract.IMPLEMENTATION_BASE,
            "planning_provenance": contract.PLANNING_PROVENANCE,
            "success": True,
            "release_grade": not contracts_only,
            "test_payload": bool(contracts_only),
            "profile": profile,
            "canonical_artifact": {
                "executable_bytes": len(canonical_executable),
                "executable_sha256": hashlib.sha256(canonical_executable).hexdigest(),
                "package_bytes": len(package_bytes),
                "package_sha256": hashlib.sha256(package_bytes).hexdigest(),
                "manifest_bytes": len(manifest_bytes),
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "artifact_key": result.common_artifact_key,
                "link_manifest": json.loads(manifest_bytes.decode("utf-8")),
            },
            "compact_artifact": {
                "executable_bytes": len(compact_executable),
                "executable_sha256": hashlib.sha256(compact_executable).hexdigest(),
                "selected_executable_bytes": len(selected_executable),
                "selected_executable_sha256": selected_sha256,
                "selected_mode": (
                    "canonical"
                    if selected_executable == canonical_executable
                    else "compact"
                ),
                "encoded_package_bytes": len(encoded.bytes),
                "encoded_package_sha256": hashlib.sha256(encoded.bytes).hexdigest(),
                "codec_id": encoded.codec_id,
                "layout_id": encoded.layout_id,
                "decoded_package_bytes": encoded.decoded_size,
                "reference_records": encoded.reference_count,
                "decoded_package_sha256": encoded.decoded_sha256,
            },
            "ledgers": {
                "canonical": canonical_ledger,
                "compact": compact_ledger,
            },
            "measurements": measurement,
            "alternatives": {
                "minimum_measured_candidate_bytes": min(
                    canonical_ledger["baseline_complete_distribution_bytes"],
                    compact_ledger["complete_runnable_distribution_bytes"],
                ),
                "canonical_complete_distribution_bytes": canonical_ledger[
                    "baseline_complete_distribution_bytes"
                ],
                "compact_complete_distribution_bytes": compact_ledger[
                    "complete_runnable_distribution_bytes"
                ],
                "compression_increases_distribution": compact_ledger[
                    "compression_rejected_for_increasing_distribution"
                ],
            },
            "visual_validation": {
                "tolerances": contract.image_tolerances(),
                "status": "pending_physical_windows_capture",
                "protocol": nw07_export.contract.CORRECTNESS_CAPTURE_PROTOCOL,
            },
            "performance_validation": {
                "contract_id": contract.PERF_CONTRACT_ID,
                "protocol": contract.PERF_PROTOCOL_ID,
                "status": "pending_physical_windows_capture",
                "runs": contract.PERF_RUN_COUNT,
                "warmup_seconds": contract.WARMUP_SECONDS,
                "run_seconds": contract.PERF_RUN_SECONDS,
            },
            "startup_validation": {
                "protocol": nw07_export.contract.STARTUP_PROTOCOL,
                "ceiling_ms": contract.STARTUP_CEILING_MS,
                "status": "pending_physical_windows_capture",
            },
            "memory_validation": {
                "ceiling_bytes": contract.PEAK_ENGINE_COMMITTED_GPU_BYTES,
                "status": "pending_physical_windows_capture",
            },
            "clean_system_validation": {
                "status": "pending_physical_windows_capture",
                "requires": "clean Windows x64 target without developer tools",
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
                    "Contract-only export and compact payload round-trip are measured; "
                    "physical Windows x64 startup, clean-system, visual, and frame-time "
                    "validation remains pending."
                ),
                "universal_64k_claim": False,
            },
        }
        # Preserve the canonical output directory verbatim; publish the compact
        # runnable candidate beside it as a separate, fully attributed closure.
        if output_path.exists():
            if not output_path.is_dir() or any(output_path.iterdir()):
                raise IntroContractError(
                    f"intro output directory is not empty: {output_path}"
                )
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
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "build" / "nw10-intro")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--player-template", type=Path)
    parser.add_argument("--dxc")
    parser.add_argument("--contracts-only", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
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
    if args.validate_only:
        print(json.dumps(validate_intro_graph(), indent=2, sort_keys=True))
        return 0
    try:
        report = export_intro(
            args.output,
            player_template=args.player_template,
            dxc_executable=args.dxc,
            report_path=args.report,
            contracts_only=args.contracts_only,
            codec_id=args.codec,
            layout_id=args.layout,
        )
    except Exception as exc:
        print(f"NW-10 intro export failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
