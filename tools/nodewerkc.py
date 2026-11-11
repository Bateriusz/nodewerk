#!/usr/bin/env python3
"""`nodewerkc` NW-03 commands: validate, compile-package, and inspect.

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
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import nw03_compiler  # noqa: E402
import nwp_package  # noqa: E402


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
    payload = {
        "ok": not errors and document is not None,
        "valid": not errors and document is not None,
        "profile_id": document.profile_id if document else "",
        "requirements_digest": document.requirements_digest if document else "",
        "semantic_digest": document.semantic_digest() if document else "",
        "node_count": len(document.nodes) if document else 0,
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
    try:
        report = nw03_compiler.compile_package(
            args.graph,
            args.output,
            report_path=args.report,
            dev_sidecar_path=args.dev_sidecar,
            cache_dir=args.cache_dir,
            dxc_executable=args.dxc,
            optimization=args.optimization,
            allow_unpinned_dxc=args.allow_unpinned_dxc,
        )
    except nw03_compiler.CompilerError as exc:
        _emit(_error_payload(str(exc), exc.diagnostics), stream=sys.stderr)
        return 1
    except nwp_package.PackageValidationError as exc:
        _emit(_error_payload(str(exc), exc.diagnostics), stream=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        _emit(_error_payload(f"compilation failed: {exc}"), stream=sys.stderr)
        return 1
    _emit(
        {
            "ok": True,
            "valid": True,
            "output": str(args.output),
            "report": report,
        }
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
