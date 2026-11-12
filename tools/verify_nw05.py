#!/usr/bin/env python3
"""NW-05 verification entry point.

Usage:
    python tools/verify_nw05.py --mode ContractsOnly [--summary-json out.json]
    python tools/verify_nw05.py --mode Full --fixture <player.exe> \
        --package <export-dir> [--summary-json out.json]
    python tools/verify_nw05.py --mode Hardware --fixture <player.exe> \
        --package <export-dir> [--summary-json out.json]

ContractsOnly is GPU-less and host-neutral.  It exercises whole-graph
optimisation, scheduling, alias property checks, export closure and budgets,
failure recovery, size attribution, and two-clean-directory reproducibility.
Windows native and Ada/Blackwell GPU checks are reported distinctly as passed,
failed, or pending; missing coverage is never treated as success.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw03_compiler  # noqa: E402
import nw03_oracle  # noqa: E402
import nw04_fixture  # noqa: E402
import nwp_package as nwp  # noqa: E402
import nw05_export  # noqa: E402
import nw05_optimizer as optimizer  # noqa: E402
import nw05_schedule as schedule  # noqa: E402
from schema_validator import validate_file_against_schema  # noqa: E402


PROCEDURAL_FIXTURE = REPO_ROOT / "fixtures" / "nw05" / "procedural_image.nwgraph"
EXPORT_FIXTURE = REPO_ROOT / "fixtures" / "nw05" / "procedural_export.nwgraph"
OPTIMIZATION_FIXTURE = REPO_ROOT / "fixtures" / "nw05" / "optimization_semantics.nwgraph"
GOLDEN_IR = REPO_ROOT / "fixtures" / "nw05" / "golden_optimized_ir.json"


@dataclass
class CheckResult:
    name: str
    required: bool
    passed: bool
    detail: str = ""
    unavailable: bool = False
    status: str = "passed"
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "required": self.required,
            "passed": self.passed,
            "unavailable": self.unavailable,
            "status": self.status,
            "detail": self.detail,
            "evidence": self.evidence,
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_mock_dxc(directory: Path, *, fail: bool = False) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    dxc = directory / "mock_dxc.py"
    container = nw04_fixture.make_dxil_container()
    dxc.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print('dxcompiler.dll: 1.8 - 1.8.2407.12')\n"
        "    sys.exit(0)\n"
        + (
            "sys.stderr.write('mock DXC failure\\n')\nsys.exit(1)\n"
            if fail
            else (
                "fo = Path(sys.argv[sys.argv.index('-Fo') + 1])\n"
                "fo.parent.mkdir(parents=True, exist_ok=True)\n"
                f"fo.write_bytes(bytes.fromhex({container.hex()!r}))\n"
            )
        ),
        encoding="utf-8",
    )
    dxc.chmod(0o755)
    return dxc


def synthetic_alias_package() -> nwp.NwpPackage:
    """Minimal model with two disjoint-lifetime transient UAV buffers."""
    resource = nwp.ResourceDescriptor
    instruction = nwp.Instruction
    operand = nwp.InstructionOperand
    return nwp.NwpPackage(
        resources=[
            resource(
                1,
                "scratch_a",
                "buffer",
                "DXGI_FORMAT_R32_UINT",
                "uav",
                "compute.math_noise",
                size_bytes=1024,
            ),
            resource(
                2,
                "scratch_b",
                "buffer",
                "DXGI_FORMAT_R32_UINT",
                "uav",
                "compute.math_noise",
                size_bytes=4096,
            ),
        ],
        initialization=[
            instruction("init.create_resource", (operand("resource_ref", 1),)),
            instruction("init.create_resource", (operand("resource_ref", 2),)),
        ],
        frame=[
            instruction(
                "frame.copy_buffer",
                (
                    operand("resource_ref", 1),
                    operand("resource_ref", 1),
                    operand("u64", 64),
                ),
            ),
            instruction(
                "frame.copy_buffer",
                (
                    operand("resource_ref", 2),
                    operand("resource_ref", 2),
                    operand("u64", 64),
                ),
            ),
        ],
    )


def _optimized(path: Path) -> optimizer.OptimizedGraph:
    document = nw03_compiler.load_graph(path)
    return optimizer.optimize_graph(document)


def check_optimization_golden() -> CheckResult:
    try:
        optimized = _optimized(PROCEDURAL_FIXTURE)
        payload = optimized.to_dict()
        golden = json.loads(GOLDEN_IR.read_text(encoding="utf-8"))
        mismatches = [
            key
            for key, value in golden.items()
            if payload.get(key) != value
        ]
        if mismatches:
            return CheckResult(
                "nw05_optimization_golden",
                True,
                False,
                f"golden mismatches: {mismatches}",
            )
        return CheckResult(
            "nw05_optimization_golden",
            True,
            True,
            evidence={
                "ir_sha256": optimized.ir_sha256(),
                "golden_keys": sorted(golden),
            },
        )
    except Exception as exc:
        return CheckResult("nw05_optimization_golden", True, False, str(exc))


def check_constant_and_partitions() -> CheckResult:
    try:
        optimized = _optimized(OPTIMIZATION_FIXTURE)
        constants = {
            node.node_id: node.constant
            for node in optimized.nodes
            if node.constant is not None
        }
        if "roughness" not in constants or "scaled" not in constants:
            return CheckResult(
                "nw05_constant_propagation",
                True,
                False,
                f"expected folded constants, got {sorted(constants)}",
            )
        if constants["roughness"]["out"] != (0.0,):
            return CheckResult(
                "nw05_constant_propagation",
                True,
                False,
                f"unexpected folded roughness {constants['roughness']}",
            )
        partitions = optimized.partitions
        if any(
            node_id not in partitions["compile-time"]
            for node_id in constants
        ):
            return CheckResult(
                "nw05_constant_propagation",
                True,
                False,
                "folded constants were not scheduled at compile time",
            )
        if not partitions["initialization"]:
            return CheckResult(
                "nw05_constant_propagation",
                True,
                False,
                "initialization partition is empty",
            )
        node_map = optimized.node_map()
        for partition_name in ("compile-time", "initialization", "frame"):
            position = {
                node_id: index
                for index, node_id in enumerate(partitions[partition_name])
            }
            for node_id in partitions[partition_name]:
                for item in node_map[node_id].inputs:
                    if item.source_node in position and position[item.source_node] > position[node_id]:
                        return CheckResult(
                            "nw05_constant_propagation",
                            True,
                            False,
                            f"partition '{partition_name}' is not dependency ordered",
                        )
        return CheckResult(
            "nw05_constant_propagation",
            True,
            True,
            evidence={
                "constants": {
                    key: [list(value) for value in outputs.values()]
                    for key, outputs in sorted(constants.items())
                },
                "partitions": {
                    key: list(value) for key, value in partitions.items()
                },
            },
        )
    except Exception as exc:
        return CheckResult("nw05_constant_propagation", True, False, str(exc))


def check_dead_and_cse() -> CheckResult:
    try:
        optimized = _optimized(OPTIMIZATION_FIXTURE)
        if "unused_curve" not in optimized.dead_nodes:
            return CheckResult(
                "nw05_dead_code_and_cse",
                True,
                False,
                "unused operator was not eliminated",
            )
        cse_pairs = {
            (record.canonical_node, merged)
            for record in optimized.cse
            for merged in record.merged_nodes
        }
        if ("shade_a", "shade_b") not in cse_pairs:
            return CheckResult(
                "nw05_dead_code_and_cse",
                True,
                False,
                f"expected CSE of shade_a/shade_b, got {sorted(cse_pairs)}",
            )
        fullscreen_b = optimized.node_map()["fullscreen_b"]
        shader_input = next(item for item in fullscreen_b.inputs if item.port == "shader")
        if shader_input.source_node != "shade_a":
            return CheckResult(
                "nw05_dead_code_and_cse",
                True,
                False,
                "CSE did not rewrite the duplicate consumer",
            )
        return CheckResult(
            "nw05_dead_code_and_cse",
            True,
            True,
            evidence={
                "dead_nodes": list(optimized.dead_nodes),
                "cse": [record.to_dict() for record in optimized.cse],
                "dead_outputs": [f"{n}.{p}" for n, p in optimized.dead_outputs],
            },
        )
    except Exception as exc:
        return CheckResult("nw05_dead_code_and_cse", True, False, str(exc))


def check_quantization() -> CheckResult:
    try:
        document = nw03_compiler.load_graph(OPTIMIZATION_FIXTURE)
        quantized = optimizer.quantize_document(document, document.settings)
        if not quantized.decisions:
            return CheckResult(
                "nw05_quantization", True, False, "no quantization decisions"
            )
        decision = quantized.decisions[0]
        if not decision.within_tolerance:
            return CheckResult(
                "nw05_quantization",
                True,
                False,
                "quantized field exceeded its declared tolerance",
            )
        recovered = quantized.recover_full_precision()
        key = f"{decision.node_id}.{decision.parameter}"
        if recovered.get(key) != decision.original:
            return CheckResult(
                "nw05_quantization",
                True,
                False,
                "full-precision recovery failed",
            )
        disabled_settings = dict(document.settings)
        disabled_settings["quantization_policy"] = "none"
        disabled = optimizer.quantize_document(document, disabled_settings)
        if disabled.decisions:
            return CheckResult(
                "nw05_quantization",
                True,
                False,
                "quantization could not be disabled",
            )
        tight = optimizer.quantize_document(
            document, document.settings, tolerance_override=0.0
        )
        if not tight.errors:
            return CheckResult(
                "nw05_quantization",
                True,
                False,
                "tolerance violation did not fail closed",
            )
        return CheckResult(
            "nw05_quantization",
            True,
            True,
            evidence={
                "decision": decision.to_dict(),
                "recovered": {key: list(decision.original)},
                "tight_tolerance_errors": list(tight.errors),
            },
        )
    except Exception as exc:
        return CheckResult("nw05_quantization", True, False, str(exc))


def check_schedule_and_alias_properties() -> CheckResult:
    try:
        package = synthetic_alias_package()
        single = schedule.build_logical_schedule(package, frames_in_flight=1)
        if single.queue_count != 1 or single.queue != "direct":
            return CheckResult(
                "nw05_schedule_alias_properties",
                True,
                False,
                "schedule is not single direct queue",
            )
        if single.to_dict()["physical_offsets_present"]:
            return CheckResult(
                "nw05_schedule_alias_properties",
                True,
                False,
                "logical schedule leaked physical offsets",
            )
        if not all(item.alias_eligible for item in single.resources):
            return CheckResult(
                "nw05_schedule_alias_properties",
                True,
                False,
                "disjoint transient resources are not alias eligible",
            )
        real = schedule.realize_physical_aliases(
            single,
            package,
            frames_in_flight=1,
            allocation_query=schedule.simulate_allocation,
        )
        aliased = [item for item in real.placements if item.aliased]
        if not aliased or not real.barriers:
            return CheckResult(
                "nw05_schedule_alias_properties",
                True,
                False,
                "compatible alias group was not realised with barriers",
            )
        if real.to_dict()["portable"]:
            return CheckResult(
                "nw05_schedule_alias_properties",
                True,
                False,
                "physical realization claimed to be portable",
            )
        # Overlapping lifetimes are never aliased.
        first, second = single.resources
        overlapping = type(first)(
            **{
                **first.__dict__,
                "resource_id": 99,
                "lifetime": type(first.lifetime)(
                    0, 1, 0, 1, False, "overlap test"
                ),
            }
        )
        compatible, _reason = schedule.compatible_alias_group(
            first, overlapping, frames_in_flight=1
        )
        if compatible:
            return CheckResult(
                "nw05_schedule_alias_properties",
                True,
                False,
                "overlapping lifetimes were accepted for aliasing",
            )
        # Different formats/groups are incompatible.
        incompatible = type(first)(
            **{
                **first.__dict__,
                "resource_id": 100,
                "format": "DXGI_FORMAT_R32G32B32A32_FLOAT",
                "alias_group": "buffer:DXGI_FORMAT_R32G32B32A32_FLOAT:uav",
            }
        )
        compatible, _reason = schedule.compatible_alias_group(
            first, incompatible, frames_in_flight=1
        )
        if compatible:
            return CheckResult(
                "nw05_schedule_alias_properties",
                True,
                False,
                "incompatible alias group was accepted",
            )
        # Frames in flight duplicate transient storage per slot.
        multi = schedule.build_logical_schedule(package, frames_in_flight=2)
        real_multi = schedule.realize_physical_aliases(
            multi,
            package,
            frames_in_flight=2,
            allocation_query=schedule.simulate_allocation,
        )
        instances = {item.resource_id: set() for item in real_multi.placements}
        for item in real_multi.placements:
            instances[item.resource_id].add(item.instance)
        if any(values != {0, 1} for values in instances.values()):
            return CheckResult(
                "nw05_schedule_alias_properties",
                True,
                False,
                "transients were not duplicated across frame slots",
            )
        if any(
            left.instance != right.instance
            for left in real_multi.placements
            for right in real_multi.placements
            if left.aliased and right.aliased
            and left.resource_id != right.resource_id
        ):
            return CheckResult(
                "nw05_schedule_alias_properties",
                True,
                False,
                "cross-slot alias reuse was emitted",
            )
        return CheckResult(
            "nw05_schedule_alias_properties",
            True,
            True,
            evidence={
                "single_queue": single.to_dict(),
                "physical_realization": real.to_dict(),
                "frames_in_flight_realization": real_multi.to_dict(),
            },
        )
    except Exception as exc:
        return CheckResult("nw05_schedule_alias_properties", True, False, str(exc))


def _export_once(
    directory: Path,
    *,
    dxc: Path,
    template: Path,
    byte_ceiling: int = 1024 * 1024,
) -> tuple[nw05_export.ExportResult, dict[str, str]]:
    settings = nw05_export.ExportSettings(
        byte_ceiling=byte_ceiling,
        memory_budget=256 * 1024 * 1024,
        player_template=str(template),
        frames_in_flight=2,
    )
    result = nw05_export.export_graph(
        EXPORT_FIXTURE,
        directory,
        settings=settings,
        report_path=directory.parent / f"{directory.name}-report.json",
        dxc_executable=str(dxc),
        allow_test_payloads=True,
    )
    hashes: dict[str, str] = {}
    for path in sorted(directory.iterdir()):
        if path.is_file():
            hashes[path.name] = _sha256(path.read_bytes())
    return result, hashes


def check_export_closure_reproducibility() -> CheckResult:
    previous = os.environ.get("NODEWERK_NW03_TEST_DIGEST")
    os.environ["NODEWERK_NW03_TEST_DIGEST"] = "1"
    try:
        with tempfile.TemporaryDirectory(prefix="nw05-verify-") as raw:
            root = Path(raw)
            dxc = make_mock_dxc(root / "toolchain")
            template = root / "specialized_player.exe"
            template.write_bytes(b"MZ" + b"\x00" * 512)
            first, first_hashes = _export_once(
                root / "clean-a", dxc=dxc, template=template
            )
            second, second_hashes = _export_once(
                root / "clean-b", dxc=dxc, template=template
            )
            if first_hashes != second_hashes:
                return CheckResult(
                    "nw05_export_closure_reproducibility",
                    True,
                    False,
                    "clean-directory export bytes differ",
                    evidence={"first": first_hashes, "second": second_hashes},
                )
            common = first.artifacts[nwp.COMMON_PROFILE_ID]
            excluded = set(common.manifest["excluded"]["optional_implementations"])
            if "nw.proc.value_noise" not in excluded:
                return CheckResult(
                    "nw05_export_closure_reproducibility",
                    True,
                    False,
                    "unused optional implementation was not excluded",
                )
            if common.manifest["runtime_requirements"]["runtime_dxc_dependency"]:
                return CheckResult(
                    "nw05_export_closure_reproducibility",
                    True,
                    False,
                    "runtime DXC dependency leaked into the link manifest",
                )
            if common.package.capability.profile_id != nwp.COMMON_PROFILE_ID:
                return CheckResult(
                    "nw05_export_closure_reproducibility",
                    True,
                    False,
                    "common artifact profile changed",
                )
            leakage = (
                nwp.check_release_leakage(common.package_bytes)
                if common.package.flags & 1
                else []
            )
            if leakage:
                return CheckResult(
                    "nw05_export_closure_reproducibility",
                    True,
                    False,
                    f"release leakage in exported package: {leakage}",
                )
            stem = nw05_export._profile_file_stem(nwp.COMMON_PROFILE_ID)
            for generated, schema in (
                (
                    root / "clean-a" / f"{stem}.manifest.json",
                    REPO_ROOT / "schemas" / "nw05-link-manifest.schema.json",
                ),
                (
                    root / "clean-a" / f"{stem}.size.json",
                    REPO_ROOT / "schemas" / "nw05-size-report.schema.json",
                ),
            ):
                validate_file_against_schema(generated, schema)
            embedded_package, embedded_manifest = nw05_export.extract_embedded_package(
                (root / "clean-a" / common.executable_name).read_bytes()
            )
            if embedded_package != common.package_bytes:
                return CheckResult(
                    "nw05_export_closure_reproducibility",
                    True,
                    False,
                    "embedded package differs from the published artifact",
                )
            return CheckResult(
                "nw05_export_closure_reproducibility",
                True,
                True,
                evidence={
                    "hashes": first_hashes,
                    "artifact_key": first.common_artifact_key,
                    "manifest_sha256": embedded_manifest and _sha256(embedded_manifest),
                    "package_sha256": _sha256(common.package_bytes),
                },
            )
    except Exception as exc:
        return CheckResult(
            "nw05_export_closure_reproducibility", True, False, str(exc)
        )
    finally:
        if previous is None:
            os.environ.pop("NODEWERK_NW03_TEST_DIGEST", None)
        else:
            os.environ["NODEWERK_NW03_TEST_DIGEST"] = previous


def check_budget_recovery_and_size() -> CheckResult:
    previous = os.environ.get("NODEWERK_NW03_TEST_DIGEST")
    os.environ["NODEWERK_NW03_TEST_DIGEST"] = "1"
    try:
        with tempfile.TemporaryDirectory(prefix="nw05-budget-") as raw:
            root = Path(raw)
            dxc = make_mock_dxc(root / "toolchain")
            template = root / "player.exe"
            template.write_bytes(b"MZ" + b"\x00" * 128)
            output = root / "artifacts"
            result, _hashes = _export_once(
                output, dxc=dxc, template=template, byte_ceiling=1024 * 1024
            )
            before = {
                path.name: _sha256(path.read_bytes())
                for path in sorted(output.iterdir())
                if path.is_file()
            }
            report_path = root / "failure-report.json"
            try:
                _export_once(
                    output,
                    dxc=dxc,
                    template=template,
                    byte_ceiling=1,
                )
            except nw05_export.ExportError:
                pass
            else:
                return CheckResult(
                    "nw05_budget_recovery_and_size",
                    True,
                    False,
                    "byte-ceiling violation did not fail closed",
                )
            after = {
                path.name: _sha256(path.read_bytes())
                for path in sorted(output.iterdir())
                if path.is_file()
            }
            if before != after:
                return CheckResult(
                    "nw05_budget_recovery_and_size",
                    True,
                    False,
                    "failed export replaced the prior complete artifact",
                )
            diagnostic_path = root / "artifacts-report.json"
            if not diagnostic_path.is_file():
                return CheckResult(
                    "nw05_budget_recovery_and_size",
                    True,
                    False,
                    "failed export did not retain a diagnostic report",
                )
            diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
            if diagnostic.get("success") is not False:
                return CheckResult(
                    "nw05_budget_recovery_and_size",
                    True,
                    False,
                    "diagnostic report did not record the failure",
                )
            common_path = output / f"{nw05_export._profile_file_stem(nwp.COMMON_PROFILE_ID)}.nwp"
            size = nw05_export.inspect_size(common_path)
            if not size["sections"] or size["content"]["shader_count"] < 1:
                return CheckResult(
                    "nw05_budget_recovery_and_size",
                    True,
                    False,
                    "inspect-size did not attribute package bytes",
                )
            return CheckResult(
                "nw05_budget_recovery_and_size",
                True,
                True,
                evidence={
                    "preserved_hashes": after,
                    "inspect_size": size,
                    "artifact_key": result.common_artifact_key,
                },
            )
    except Exception as exc:
        return CheckResult("nw05_budget_recovery_and_size", True, False, str(exc))
    finally:
        if previous is None:
            os.environ.pop("NODEWERK_NW03_TEST_DIGEST", None)
        else:
            os.environ["NODEWERK_NW03_TEST_DIGEST"] = previous


def check_optional_profiles_independent() -> CheckResult:
    previous = os.environ.get("NODEWERK_NW03_TEST_DIGEST")
    os.environ["NODEWERK_NW03_TEST_DIGEST"] = "1"
    try:
        with tempfile.TemporaryDirectory(prefix="nw05-optional-") as raw:
            root = Path(raw)
            dxc = make_mock_dxc(root / "toolchain")
            template = root / "player.exe"
            template.write_bytes(b"MZ" + b"\x00" * 128)
            settings = nw05_export.ExportSettings(
                byte_ceiling=1024 * 1024,
                memory_budget=256 * 1024 * 1024,
                optional_profiles=(nwp.OPTIONAL_PROFILE_WAVE_OPS,),
                allow_optional_profiles=True,
                player_template=str(template),
            )
            result = nw05_export.export_graph(
                EXPORT_FIXTURE,
                root / "artifacts",
                settings=settings,
                dxc_executable=str(dxc),
                allow_test_payloads=True,
            )
            common = result.artifacts[nwp.COMMON_PROFILE_ID]
            optional = result.artifacts[nwp.OPTIONAL_PROFILE_WAVE_OPS]
            if common.artifact_key == optional.artifact_key:
                return CheckResult(
                    "nw05_optional_profiles_independent",
                    True,
                    False,
                    "optional profile did not receive an independent artifact key",
                )
            if optional.package.capability.optional_profile_id != nwp.OPTIONAL_PROFILE_WAVE_OPS:
                return CheckResult(
                    "nw05_optional_profiles_independent",
                    True,
                    False,
                    "optional package identity was not recorded separately",
                )
            if common.package.capability.optional_profile_id:
                return CheckResult(
                    "nw05_optional_profiles_independent",
                    True,
                    False,
                    "optional profile leaked into the common package",
                )
            if result.report["budgets"]["common_baseline_raised_by_optional"]:
                return CheckResult(
                    "nw05_optional_profiles_independent",
                    True,
                    False,
                    "optional profile raised the common budget baseline",
                )
            return CheckResult(
                "nw05_optional_profiles_independent",
                True,
                True,
                evidence={
                    "common_key": common.artifact_key,
                    "optional_key": optional.artifact_key,
                    "budgets": result.report["budgets"],
                },
            )
    except Exception as exc:
        return CheckResult("nw05_optional_profiles_independent", True, False, str(exc))
    finally:
        if previous is None:
            os.environ.pop("NODEWERK_NW03_TEST_DIGEST", None)
        else:
            os.environ["NODEWERK_NW03_TEST_DIGEST"] = previous


def _pending(name: str, detail: str) -> CheckResult:
    return CheckResult(
        name,
        False,
        False,
        detail,
        True,
        status="pending",
    )


def run_contracts_only() -> list[CheckResult]:
    return [
        check_optimization_golden(),
        check_constant_and_partitions(),
        check_dead_and_cse(),
        check_quantization(),
        check_schedule_and_alias_properties(),
        check_export_closure_reproducibility(),
        check_budget_recovery_and_size(),
        check_optional_profiles_independent(),
    ]


def _windows_checks(args: argparse.Namespace) -> list[CheckResult]:
    if not args.fixture or not args.package:
        return [
            _pending(
                "nw05_windows_native",
                "No specialized player executable/package supplied.",
            ),
            _pending(
                "nw05_ada_blackwell_gpu_validation",
                "No representative Ada/Blackwell execution coverage is recorded.",
            ),
        ]
    fixture = Path(args.fixture)
    package = Path(args.package)
    if not fixture.is_file() or not package.exists():
        return [
            CheckResult(
                "nw05_windows_native",
                True,
                False,
                "supplied native fixture/package is missing",
                status="failed",
            ),
            _pending(
                "nw05_ada_blackwell_gpu_validation",
                "Native artifact unavailable; GPU coverage remains pending.",
            ),
        ]
    common_stem = nw05_export._profile_file_stem(nwp.COMMON_PROFILE_ID)
    package_path = (
        package / f"{common_stem}.nwp" if package.is_dir() else package
    )
    if not package_path.is_file():
        return [
            CheckResult(
                "nw05_windows_native",
                True,
                False,
                f"common package is missing: {package_path}",
                status="failed",
            ),
            _pending(
                "nw05_ada_blackwell_gpu_validation",
                "Common artifact unavailable; GPU coverage remains pending.",
            ),
        ]
    package_bytes = package_path.read_bytes()
    try:
        embedded_package, embedded_manifest = nw05_export.extract_embedded_package(
            fixture.read_bytes()
        )
    except nw05_export.ExportError as exc:
        return [
            CheckResult(
                "nw05_windows_native",
                True,
                False,
                f"specialized player has no valid embedded package: {exc}",
                status="failed",
            ),
            _pending(
                "nw05_ada_blackwell_gpu_validation",
                "Native artifact unavailable; GPU coverage remains pending.",
            ),
        ]
    if embedded_package != package_bytes:
        return [
            CheckResult(
                "nw05_windows_native",
                True,
                False,
                "executed player embeds a different common artifact",
                status="failed",
            ),
            _pending(
                "nw05_ada_blackwell_gpu_validation",
                "Common artifact mismatch; GPU coverage remains pending.",
            ),
        ]
    try:
        completed = subprocess.run(
            [str(fixture), "--frames", "1"],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [
            CheckResult(
                "nw05_windows_native",
                True,
                False,
                f"native execution failed to start: {exc}",
                status="failed",
            ),
            _pending(
                "nw05_ada_blackwell_gpu_validation",
                "Native execution unavailable; GPU coverage remains pending.",
            ),
        ]
    native_passed = completed.returncode == 0
    native = CheckResult(
        "nw05_windows_native",
        True,
        native_passed,
        (
            "specialized player executed the identical common artifact"
            if native_passed
            else f"specialized player exited {completed.returncode}"
        ),
        status="passed" if native_passed else "failed",
        evidence={
            "exit_code": completed.returncode,
            "package_sha256": _sha256(package_bytes),
            "executable_sha256": _sha256(fixture.read_bytes()),
            "manifest_sha256": _sha256(embedded_manifest),
            "stdout_sha256": _sha256(
                completed.stdout.encode("utf-8", errors="replace")
            ),
            "stderr_sha256": _sha256(
                completed.stderr.encode("utf-8", errors="replace")
            ),
        },
    )
    evidence_path = Path(args.evidence) if getattr(args, "evidence", None) else None
    if evidence_path is not None and evidence_path.is_file():
        try:
            record = json.loads(evidence_path.read_text(encoding="utf-8"))
            embedded_manifest_payload = json.loads(
                embedded_manifest.decode("utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            gpu = CheckResult(
                "nw05_ada_blackwell_gpu_validation",
                True,
                False,
                f"evidence/manifest is not valid JSON: {exc}",
                status="failed",
            )
            return [native, gpu]
        embedded_artifact_key = str(
            embedded_manifest_payload.get("artifact_identity", "")
        )
        embedded_package_sha = _sha256(package_bytes)
        statuses = {
            tier: record.get("qualifications", {}).get(tier, {}).get("status")
            for tier in ("ada", "blackwell")
        }
        keys = {
            record.get("qualifications", {}).get(tier, {}).get("artifact_key")
            for tier in ("ada", "blackwell")
        }
        package_hashes = {
            record.get("qualifications", {})
            .get(tier, {})
            .get("package_sha256")
            for tier in ("ada", "blackwell")
        }
        executable_hashes = {
            record.get("qualifications", {})
            .get(tier, {})
            .get("executable_sha256")
            for tier in ("ada", "blackwell")
        }
        adapter_descriptions = {
            record.get("qualifications", {})
            .get(tier, {})
            .get("adapter_description")
            for tier in ("ada", "blackwell")
        }
        executions_by_tier = {
            str(execution.get("hardware_tier")): execution
            for execution in record.get("executions", [])
            if isinstance(execution, dict)
        }
        telemetry_ok = True
        for tier in ("ada", "blackwell"):
            execution = executions_by_tier.get(tier, {})
            details = execution.get("details", {})
            if (
                execution.get("execution_status") != "passed"
                or not details.get("device")
                or not details.get("adapter_description")
                or not details.get("driver_version")
                or not details.get("capabilities")
                or not execution.get("hardware_identity_verified")
                or not execution.get("validation_enabled")
                or not record.get("qualifications", {})
                .get(tier, {})
                .get("hardware_identity_verified")
                or not execution.get("aliased_plan_realized")
                or int(execution.get("alias_barrier_count", 0) or 0) <= 0
                or int(execution.get("alias_activations", 0) or 0) <= 0
                or not execution.get("output_matches_non_aliased")
                or not record.get("toolchain_manifest_sha256")
            ):
                telemetry_ok = False
        identity_mismatch = (
            keys != {embedded_artifact_key}
            or package_hashes != {embedded_package_sha}
            or executable_hashes
            != {_sha256(fixture.read_bytes())}
        )
        if any(status == "failed" for status in statuses.values()):
            gpu = CheckResult(
                "nw05_ada_blackwell_gpu_validation",
                True,
                False,
                "Ada/Blackwell evidence contains an executed failure",
                status="failed",
                evidence={"statuses": statuses, "artifact_keys": sorted(keys)},
            )
        elif all(status == "verified" for status in statuses.values()) and (
            identity_mismatch or not telemetry_ok
        ):
            gpu = CheckResult(
                "nw05_ada_blackwell_gpu_validation",
                True,
                False,
                (
                    "verified evidence does not match the executed artifact or "
                    "is missing device/capability telemetry"
                ),
                status="failed",
                evidence={
                    "statuses": statuses,
                    "artifact_keys": sorted(str(value) for value in keys),
                    "expected_artifact_key": embedded_artifact_key,
                    "package_hashes": sorted(str(value) for value in package_hashes),
                    "expected_package_sha256": embedded_package_sha,
                    "executable_hashes": sorted(
                        str(value) for value in executable_hashes
                    ),
                    "expected_executable_sha256": _sha256(fixture.read_bytes()),
                    "adapter_descriptions": sorted(
                        str(value) for value in adapter_descriptions
                    ),
                    "telemetry_ok": telemetry_ok,
                },
            )
        elif all(status == "verified" for status in statuses.values()):
            gpu = CheckResult(
                "nw05_ada_blackwell_gpu_validation",
                True,
                True,
                "Ada and Blackwell evidence reference the identical common artifact",
                status="passed",
                evidence={
                    "artifact_key": embedded_artifact_key,
                    "evidence": str(evidence_path),
                },
            )
        else:
            gpu = _pending(
                "nw05_ada_blackwell_gpu_validation",
                "Missing Ada/Blackwell coverage remains pending.",
            )
    else:
        gpu = _pending(
            "nw05_ada_blackwell_gpu_validation",
            "GPU validation must be executed by the NW-01B evidence harness.",
        )
    return [
        native,
        gpu,
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NW-05 verification entry point")
    parser.add_argument(
        "--mode",
        choices=["ContractsOnly", "Full", "Hardware"],
        default="ContractsOnly",
    )
    parser.add_argument("--fixture")
    parser.add_argument("--package")
    parser.add_argument("--evidence")
    parser.add_argument("--summary-json")
    args = parser.parse_args(argv)

    checks = run_contracts_only()
    platform_checks = _windows_checks(args)
    all_checks = checks + platform_checks
    required_failures = [
        check
        for check in checks
        if check.required and not check.passed
    ]
    if args.mode != "ContractsOnly":
        required_failures.extend(
            check
            for check in platform_checks
            if check.status == "failed"
        )
    gpu_status = next(
        (
            check.status
            for check in platform_checks
            if check.name == "nw05_ada_blackwell_gpu_validation"
        ),
        "pending",
    )
    windows_status = next(
        (
            check.status
            for check in platform_checks
            if check.name == "nw05_windows_native"
        ),
        "pending",
    )
    def evidence_status(status: str) -> str:
        return "pending_unverified" if status == "pending" else status

    summary = {
        "format": "nw05-verification-summary",
        "version": 1,
        "mode": args.mode,
        "checks": [check.to_dict() for check in all_checks],
        "contracts_passed": not any(
            check.required and not check.passed for check in checks
        ),
        "windows_native": windows_status,
        "gpu_ada_blackwell": gpu_status,
        "gpu": gpu_status,
        "windows_native_status": evidence_status(windows_status),
        "gpu_status": evidence_status(gpu_status),
        "reproducibility": next(
            (
                check.evidence
                for check in checks
                if check.name == "nw05_export_closure_reproducibility"
            ),
            {},
        ),
        "policy": (
            "Missing Windows/Ada/Blackwell coverage remains pending; executed "
            "failures remain failures. GPU metadata never selects targets."
        ),
    }
    summary["passed"] = not required_failures
    if args.summary_json:
        Path(args.summary_json).write_text(
            json.dumps(summary, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    print(json.dumps(summary, indent=2, sort_keys=True))
    if not summary["contracts_passed"]:
        return 1
    if args.mode != "ContractsOnly":
        if any(check.status == "failed" for check in platform_checks):
            return 1
        if args.mode == "Hardware" and any(
            check.status == "pending" for check in platform_checks
        ):
            return 3
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

