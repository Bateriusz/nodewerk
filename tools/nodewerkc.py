#!/usr/bin/env python3
"""`nodewerkc` NW-03/NW-05 commands: validate, compile-package, export,
inspect, and inspect-size.

Invocation:
    python tools/nodewerkc.py validate <graph.nwgraph> [--json]
    python tools/nodewerkc.py compile-package <graph.nwgraph> <out.nwp>
        [--report report.json] [--dev-sidecar sidecar.json]
        [--cache-dir cache/] [--dxc /path/to/dxc] [--optimization O2]
    python tools/nodewerkc.py inspect <package.nwp> [--json]

`validate` and `inspect` always emit machine-readable JSON on stdout.  Compile
diagnostics are emitted as JSON on stderr.  Any required-check failure exits
nonzero and never publishes a partial package.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import nw03_compiler  # noqa: E402
import nwp_package  # noqa: E402
import nw05_export  # noqa: E402
import nw05_optimizer  # noqa: E402


def _emit(payload: object, *, stream: object = sys.stdout) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True), file=stream)  # type: ignore[arg-type]


def _error_payload(message: str, diagnostics: object | None = None) -> dict[str, object]:
    return {
        "ok": False,
        "valid": False,
        "error": message,
        "diagnostics": diagnostics if diagnostics is not None else [],
    }


def command_validate(args: argparse.Namespace) -> int:
    try:
        text = Path(args.graph).read_text(encoding="utf-8")
    except OSError as exc:
        _emit(_error_payload(f"cannot read graph '{args.graph}': {exc}"))
        return 1
    document, diagnostics = nw03_compiler.parse_graph_text(
        text, source_name=str(args.graph)
    )
    errors = [item for item in diagnostics if item.get("severity") == "error"]
    if errors or document is None:
        _emit(
            {
                "ok": False,
                "valid": False,
                "profile_id": document.profile_id if document else "",
                "requirements_digest": document.requirements_digest if document else "",
                "semantic_digest": "",
                "node_count": len(document.nodes) if document else 0,
                "diagnostics": diagnostics,
            }
        )
        return 1
    try:
        semantic_digest = document.semantic_digest()
    except nw03_compiler.CompilerError as exc:
        _emit(_error_payload(str(exc), exc.diagnostics))
        return 1
    payload = {
        "ok": True,
        "valid": True,
        "profile_id": document.profile_id,
        "requirements_digest": document.requirements_digest,
        "semantic_digest": semantic_digest,
        "node_count": len(document.nodes),
        "diagnostics": diagnostics,
    }
    if document is not None and not errors:
        generated, lowering_diagnostics = nw03_compiler.lower_to_fused_hlsl(document)
        payload["lowering"] = {
            "specialization_count": len(generated),
            "specializations": [
                {
                    "specialization_id": shader.specialization_id,
                    "specialization_key": shader.specialization_key,
                    "entry_point": shader.entry_point,
                    "target": shader.target,
                    "hlsl_sha256": shader.hlsl_sha256,
                    "reachable_node_ids": list(shader.root_node_ids),
                }
                for shader in generated
            ],
            "diagnostics": lowering_diagnostics,
        }
        # A structurally valid graph may intentionally contain no reachable
        # ShaderExpr entry point (for example a CPU-only fixture).  Lowering
        # diagnostics are reported here, but compile-package is where a missing
        # shader entry point becomes a fatal error.
    _emit(payload)
    return 0 if payload["ok"] else 1


def command_compile_package(args: argparse.Namespace) -> int:
    graph_profile = ""
    try:
        graph = nw03_compiler.load_graph(args.graph)
        graph_profile = graph.profile_id
        if args.target_profile and args.target_profile != graph.profile_id:
            raise nw03_compiler.CompilerError(
                f"target profile '{args.target_profile}' does not match graph profile "
                f"'{graph.profile_id}'",
                [
                    nwp_package.package_diagnostic(
                        "target_profile_mismatch",
                        "the graph profile is the sole export target identity",
                    )
                ],
            )
        if args.requirements_digest and args.requirements_digest != graph.requirements_digest:
            raise nw03_compiler.CompilerError(
                "target requirements digest does not match the graph profile",
                [
                    nwp_package.package_diagnostic(
                        "target_profile_digest_mismatch",
                        "requirements digest must match the canonical graph profile",
                    )
                ],
            )
        if args.quantization_tolerance is not None:
            quantized = nw05_optimizer.quantize_document(
                graph,
                graph.settings,
                tolerance_override=args.quantization_tolerance,
            )
            if quantized.errors:
                raise nw03_compiler.CompilerError(
                    "quantization exceeds the declared tolerance",
                    [
                        nwp_package.package_diagnostic(
                            "quantization_tolerance_exceeded", message
                        )
                        for message in quantized.errors
                    ],
                )
            compile_document = quantized.document
        else:
            compile_document = graph
    except nw03_compiler.CompilerError as exc:
        payload = _error_payload(str(exc), exc.diagnostics)
        payload["profile_id"] = graph_profile
        _emit(payload, stream=sys.stderr)
        return 1
    output = Path(args.output)
    temporary_output: Path | None = None
    try:
        destination = output
        if args.byte_ceiling is not None or args.memory_budget is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary_fd, temporary_name = tempfile.mkstemp(
                prefix=".nodewerkc-budget-",
                suffix=".nwp",
                dir=str(output.parent),
            )
            # mkstemp returns an open handle; close it before the compiler
            # atomically replaces this path (required on Windows).
            os.close(temporary_fd)
            temporary_output = Path(temporary_name)
            destination = temporary_output
        report = nw03_compiler.compile_package(
            args.graph,
            destination,
            document=compile_document,
            report_path=args.report,
            dev_sidecar_path=args.dev_sidecar,
            cache_dir=args.cache_dir,
            dxc_executable=args.dxc,
            optimization=args.optimization,
            allow_unpinned_dxc=args.allow_unpinned_dxc,
            allow_test_payloads=args.allow_test_payloads,
        )
        package_bytes = destination.read_bytes()
        package = nwp_package.decode_package(
            package_bytes,
            allow_dev=not bool(package_bytes and package_bytes[16] & 1),
        )
        distributed = len(package_bytes)
        if args.byte_ceiling is not None and distributed > args.byte_ceiling:
            report["budgets"] = {
                "byte_ceiling": args.byte_ceiling,
                "memory_budget": args.memory_budget,
                "distributed_bytes": distributed,
                "resident_resource_bytes": None,
                "within_byte_ceiling": False,
                "within_memory_budget": None,
            }
            report["success"] = False
            report["diagnostics"] = [
                nwp_package.package_diagnostic(
                    "export_byte_ceiling_exceeded",
                    "package exceeds the configured byte ceiling",
                )
            ]
            report["preserved_previous_artifact"] = True
            if args.report:
                nw05_export._write_bytes(
                    Path(args.report),
                    json.dumps(report, indent=2, sort_keys=True).encode("utf-8"),
                )
            raise nw03_compiler.CompilerError(
                f"package size {distributed} exceeds byte ceiling {args.byte_ceiling}",
                report["diagnostics"],
            )
        resident_bytes = sum(resource.size_bytes for resource in package.resources)
        if args.memory_budget is not None and resident_bytes > args.memory_budget:
            report["budgets"] = {
                "byte_ceiling": args.byte_ceiling,
                "memory_budget": args.memory_budget,
                "distributed_bytes": distributed,
                "resident_resource_bytes": resident_bytes,
                "within_byte_ceiling": args.byte_ceiling is None
                or distributed <= args.byte_ceiling,
                "within_memory_budget": False,
            }
            report["success"] = False
            report["diagnostics"] = [
                nwp_package.package_diagnostic(
                    "export_memory_budget_exceeded",
                    "resident resource bytes exceed the configured memory budget",
                )
            ]
            report["preserved_previous_artifact"] = True
            if args.report:
                nw05_export._write_bytes(
                    Path(args.report),
                    json.dumps(report, indent=2, sort_keys=True).encode("utf-8"),
                )
            raise nw03_compiler.CompilerError(
                f"resource memory {resident_bytes} exceeds budget {args.memory_budget}",
                report["diagnostics"],
            )
        if temporary_output is not None:
            os.replace(temporary_output, output)
            temporary_output = None
        report["budgets"] = {
            "byte_ceiling": args.byte_ceiling,
            "memory_budget": args.memory_budget,
            "distributed_bytes": distributed,
            "resident_resource_bytes": resident_bytes,
            "within_byte_ceiling": args.byte_ceiling is None
            or distributed <= args.byte_ceiling,
            "within_memory_budget": args.memory_budget is None
            or resident_bytes <= args.memory_budget,
        }
    except nw03_compiler.CompilerError as exc:
        _emit(_error_payload(str(exc), exc.diagnostics), stream=sys.stderr)
        return 1
    except nwp_package.PackageValidationError as exc:
        _emit(_error_payload(str(exc), exc.diagnostics), stream=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        _emit(_error_payload(f"compilation failed: {exc}"), stream=sys.stderr)
        return 1
    finally:
        if temporary_output is not None:
            try:
                temporary_output.unlink()
            except OSError:
                pass
    _emit(
        {
            "ok": True,
            "valid": True,
            "output": str(args.output),
            "report": report,
        }
    )
    return 0


def _override_pairs(values: list[str] | None) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for value in values or []:
        if "=" not in value:
            raise nw05_export.ExportError(
                f"override '{value}' must be <node-id>=generate|bake"
            )
        node_id, mode = value.split("=", 1)
        result.append((node_id, mode))
    return tuple(result)


def command_export(args: argparse.Namespace) -> int:
    try:
        settings = nw05_export.ExportSettings(
            byte_ceiling=args.byte_ceiling,
            memory_budget=args.memory_budget,
            quantization_tolerance=args.quantization_tolerance,
            target_profile=args.target_profile,
            requirements_digest=args.requirements_digest or "",
            optional_profiles=tuple(args.optional_profile or ()),
            generation_overrides=_override_pairs(args.generation_override),
            bake_overrides=_override_pairs(args.bake_override),
            frames_in_flight=args.frames_in_flight,
            player_template=args.player_template or "",
            allow_optional_profiles=(
                args.allow_optional_profiles
                or bool(args.optional_profile)
                or args.target_profile != nwp_package.COMMON_PROFILE_ID
            ),
            fusion_enabled=not args.no_fusion,
            fusion_max_nodes=args.fusion_max_nodes,
            fusion_max_expression_bytes=args.fusion_max_expression_bytes,
        )
        allocation_query = None
        if args.allocation_query_json:
            raw = json.loads(Path(args.allocation_query_json).read_text(encoding="utf-8"))
            allocation_query = {
                int(key) if str(key).isdigit() else key: value
                for key, value in raw.items()
            }
        requested_output = Path(args.output)
        artifact_directory = requested_output
        requested_file: Path | None = None
        if requested_output.suffix.lower() in {".nwp", ".exe"}:
            artifact_directory = requested_output.parent / (
                requested_output.stem + ".export"
            )
            requested_file = requested_output
        if (
            requested_file is not None
            and requested_file.suffix.lower() == ".exe"
            and (not args.player_template or not Path(args.player_template).is_file())
        ):
            _emit(
                _error_payload(
                    "a self-contained executable requires an existing "
                    "--player-template"
                ),
                stream=sys.stderr,
            )
            return 1
        result = nw05_export.export_graph(
            args.graph,
            artifact_directory,
            settings=settings,
            report_path=args.report,
            dxc_executable=args.dxc,
            optimization=args.optimization,
            cache_dir=args.cache_dir,
            allow_unpinned_dxc=args.allow_unpinned_dxc,
            allow_test_payloads=args.allow_test_payloads,
            allocation_query=allocation_query,
            adapter_identity=args.adapter_identity,
        )
    except (nw05_export.ExportError, nw03_compiler.CompilerError) as exc:
        payload = _error_payload(str(exc), getattr(exc, "diagnostics", []))
        payload["dependency_path"] = getattr(exc, "dependency_path", [])
        _emit(payload, stream=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        _emit(_error_payload(f"export failed: {exc}"), stream=sys.stderr)
        return 1
    if requested_file is not None:
        common = result.artifacts[nwp_package.COMMON_PROFILE_ID]
        if requested_file.suffix.lower() == ".exe":
            if common.executable_bytes is None:
                _emit(
                    _error_payload(
                        "a self-contained executable requires --player-template"
                    ),
                    stream=sys.stderr,
                )
                return 1
            payload_bytes = common.executable_bytes
        else:
            payload_bytes = common.package_bytes
        nw05_export._write_bytes(requested_file, payload_bytes)
        nw05_export._write_bytes(
            requested_file.with_suffix(".manifest.json"),
            nw05_optimizer.manifest_bytes(common.manifest),
        )
    _emit(
        {
            "ok": True,
            "valid": True,
            "output_directory": result.output_directory,
            "artifact": str(requested_file) if requested_file is not None else "",
            "common_artifact_key": result.common_artifact_key,
            "artifacts": list(result.artifacts),
            "report": result.report,
        }
    )
    if args.link_manifest:
        common = result.artifacts[nwp_package.COMMON_PROFILE_ID]
        nw05_export._write_bytes(
            Path(args.link_manifest),
            nw05_optimizer.manifest_bytes(common.manifest),
        )
    return 0


def command_inspect(args: argparse.Namespace) -> int:
    try:
        data = Path(args.package).read_bytes()
    except OSError as exc:
        _emit(_error_payload(f"cannot read package '{args.package}': {exc}"))
        return 1
    inspection = nwp_package.inspect_package_bytes(
        data,
        allow_dev=not args.release,
        require_integrity=not args.skip_integrity,
    )
    payload = inspection.to_dict()
    if inspection.package is not None:
        leak_scan = nwp_package.leak_scan_report(data)
        payload["leak_scan"] = leak_scan
        if args.release and not leak_scan["valid"]:
            payload["valid"] = False
            payload["ok"] = False
            payload.setdefault("diagnostics", []).extend(
                nwp_package.package_diagnostic(
                    "package_release_leakage", finding
                )
                for finding in leak_scan["findings"]
            )
    _emit(payload)
    return 0 if payload["valid"] else 1


def command_inspect_size(args: argparse.Namespace) -> int:
    try:
        payload = nw05_export.inspect_size(args.path)
    except (nw05_export.ExportError, OSError, ValueError) as exc:
        _emit(_error_payload(str(exc), getattr(exc, "diagnostics", [])))
        return 1
    payload["ok"] = True
    payload["valid"] = True
    _emit(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nodewerkc",
        description="Nodewerk NW-03 whole-demo compiler and .nwp package tooling.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser(
        "validate", help="validate a .nwgraph document and its reachable shader lowering"
    )
    validate.add_argument("graph", help="path to a .nwgraph v1 document")
    validate.add_argument("--json", action="store_true", help="JSON output (the only mode)")
    validate.set_defaults(handler=command_validate)

    compile_package = subparsers.add_parser(
        "compile-package",
        help="lower, compile with offline DXC, validate, and atomically publish a .nwp package",
    )
    compile_package.add_argument("graph", help="path to a .nwgraph v1 document")
    compile_package.add_argument("output", help="destination .nwp package")
    compile_package.add_argument(
        "--report", "--build-report", help="write a development build report JSON"
    )
    compile_package.add_argument(
        "--dev-sidecar",
        "--sidecar",
        help="write a development source-map/reflection sidecar",
    )
    compile_package.add_argument(
        "--cache-dir",
        "--shader-cache",
        help="content-addressed shader cache directory",
    )
    compile_package.add_argument("--dxc", help="explicit offline DXC executable")
    compile_package.add_argument("--optimization", default="O2", choices=["O0", "O1", "O2", "O3"])
    compile_package.add_argument(
        "--allow-unpinned-dxc",
        action="store_true",
        help="developer-only override; release validation requires the pinned DXC identity",
    )
    compile_package.add_argument(
        "--allow-test-payloads",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    compile_package.add_argument(
        "--byte-ceiling",
        type=int,
        default=None,
        help="fail closed if the published package exceeds this many bytes",
    )
    compile_package.add_argument(
        "--memory-budget",
        type=int,
        default=None,
        help="fail closed if resident package resource bytes exceed this budget",
    )
    compile_package.add_argument(
        "--quantization-tolerance",
        type=float,
        default=None,
        help="maximum accepted field quantization error (metadata-only here)",
    )
    compile_package.add_argument(
        "--target-profile",
        default=nwp_package.COMMON_PROFILE_ID,
        help="target capability-profile identity",
    )
    compile_package.add_argument(
        "--requirements-digest",
        default=nwp_package.COMMON_REQUIREMENTS_DIGEST,
        help="canonical target requirements digest",
    )
    compile_package.set_defaults(handler=command_compile_package)

    inspect = subparsers.add_parser(
        "inspect", help="validate and inspect a .nwp package without GPU submission"
    )
    inspect.add_argument("package", help="path to a .nwp package")
    inspect.add_argument("--json", action="store_true", help="JSON output (the only mode)")
    inspect.add_argument(
        "--allow-dev",
        dest="release",
        action="store_false",
        help="accept development sidecar sections (default rejects them)",
    )
    inspect.add_argument(
        "--skip-integrity",
        action="store_true",
        help="development diagnostic only: decode without matching the integrity checksum",
    )
    inspect.set_defaults(handler=command_inspect, release=True)

    export = subparsers.add_parser(
        "export",
        help=(
            "optimise, schedule, compile, budget, and publish one artifact per "
            "capability profile with a generated player link manifest"
        ),
    )
    export.add_argument("graph", help="path to a .nwgraph v1 document")
    export.add_argument(
        "output", help="destination artifact directory (created transactionally)"
    )
    export.add_argument("--report", help="write the complete export report JSON")
    export.add_argument(
        "--link-manifest",
        help="write the common artifact generated player link manifest to this path",
    )
    export.add_argument("--dxc", help="explicit offline DXC executable")
    export.add_argument(
        "--optimization", default="O2", choices=["O0", "O1", "O2", "O3"]
    )
    export.add_argument("--cache-dir", help="content-addressed shader cache")
    export.add_argument("--allow-unpinned-dxc", action="store_true")
    export.add_argument("--allow-test-payloads", action="store_true", help=argparse.SUPPRESS)
    export.add_argument(
        "--byte-ceiling",
        type=int,
        default=nw05_export.DEFAULT_BYTE_CEILING,
        help="per-artifact distributed byte ceiling",
    )
    export.add_argument(
        "--memory-budget",
        type=int,
        default=nw05_export.DEFAULT_MEMORY_BUDGET,
        help="per-artifact physical peak-memory budget",
    )
    export.add_argument(
        "--quantization-tolerance",
        type=float,
        default=None,
        help="maximum accepted per-field quantization error",
    )
    export.add_argument(
        "--target-profile",
        default=nwp_package.COMMON_PROFILE_ID,
        help="target capability-profile identity",
    )
    export.add_argument(
        "--requirements-digest",
        default=None,
        help="canonical target requirements digest (defaults to the profile's canonical digest)",
    )
    export.add_argument(
        "--optional-profile",
        action="append",
        default=[],
        help="optional capability profile to emit as a separately budgeted artifact",
    )
    export.add_argument(
        "--allow-optional-profiles",
        action="store_true",
        help="allow optional profiles to be emitted separately",
    )
    export.add_argument(
        "--player-template",
        help="specialized player executable template to embed the package into",
    )
    export.add_argument(
        "--generation-override",
        action="append",
        default=[],
        metavar="NODE=generate|bake",
        help="explicit generation/bake override; no automatic oracle is applied",
    )
    export.add_argument(
        "--bake-override",
        action="append",
        default=[],
        metavar="NODE=generate|bake",
        help="explicit bake override alias",
    )
    export.add_argument(
        "--frames-in-flight",
        type=int,
        default=2,
        choices=[1, 2, 3],
        help="frame slots used for transient duplication/serialization proof",
    )
    export.add_argument(
        "--adapter-identity",
        default="unavailable-native-adapter",
        help="metadata-only adapter identity recorded with physical realization",
    )
    export.add_argument(
        "--allocation-query-json",
        help="startup allocation-query evidence keyed by resource id/name",
    )
    export.add_argument(
        "--fusion-max-nodes",
        type=int,
        default=nw05_optimizer.DEFAULT_FUSION_MAX_NODES,
    )
    export.add_argument(
        "--fusion-max-expression-bytes",
        type=int,
        default=nw05_optimizer.DEFAULT_FUSION_MAX_EXPRESSION_BYTES,
    )
    export.add_argument("--no-fusion", action="store_true")
    export.set_defaults(handler=command_export)

    inspect_size = subparsers.add_parser(
        "inspect-size",
        help="attribute all package/export distributed bytes and dependency costs",
    )
    inspect_size.add_argument("path", help=".nwp package or export directory")
    inspect_size.set_defaults(handler=command_inspect_size)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
