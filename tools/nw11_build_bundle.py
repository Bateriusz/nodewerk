#!/usr/bin/env python3
"""Build a deterministic, self-contained NW-11 validation bundle.

CI compiles the executables and then invokes this tool on those same-revision
artifacts.  The tool copies them plus the repository-owned verifier, launcher,
profile, experiment definition, schemas, shared/optional shader sources, and
graph-derived constants into a bundle, records SHA-256 identities for every
file, and writes a versioned bundle manifest.  It never edits evidence or marks
a result verified.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import shutil
import sys
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
sys.path.insert(0, str(THIS_DIR))

import nw11_bundle as bundle  # noqa: E402
import nw11_contract as contract  # noqa: E402
import nw11_experiment as experiment  # noqa: E402
import nw11_toolchain_identity as toolchain_identity  # noqa: E402
import nw11_production_binding as production_binding  # noqa: E402

TOOL_FILES = (
    "nw11_contract.py",
    "nw11_capability.py",
    "nw11_experiment.py",
    "nw11_bundle.py",
    "nw11_evidence.py",
    "nw11_followup.py",
    "nw11_verify.py",
    "nw11_toolchain_identity.py",
    "nw11_production_binding.py",
)
SUPPORT_FILES = (
    ("tools/schema_validator.py", "tools/nw11/schema_validator.py"),
    ("tools/evidence_checks.py", "tools/nw11/evidence_checks.py"),
    ("tools/nw01/tool-version-identities.json", "tools/nw11/tool-version-identities.json"),
)
SCHEMA_FILES = (
    "nw11-capability-profile.schema.json",
    "nw11-experiment.schema.json",
    "nw11-evidence.schema.json",
    "nw11-bundle-manifest.schema.json",
)
COLLECTOR = "tools/nw11/collect-evidence.py"
CAPTURE_COMPARATOR = "tools/nw11/compare-captures.py"
LAUNCHER = "tools/nw11/run-validation.ps1"


def verify_repository_commit(root: Path, commit: str) -> None:
    """Fail unless the working tree HEAD is the commit being published."""
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BuildError(f"cannot resolve repository HEAD: {exc}") from exc
    if head != commit:
        raise BuildError(
            f"bundle commit {commit} does not match repository HEAD {head}"
        )


class BuildError(ValueError):
    """Raised when the source artifacts cannot form a valid bundle."""


def _copy(source: Path, destination: Path) -> Path:
    if not source.is_file():
        raise BuildError(f"required artifact is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return destination


def build(
    *,
    output_directory: Path | str,
    experiment_id: str,
    commit: str,
    baseline_executable: Path | str,
    variant_executable: Path | str,
    fallback_executable: Path | str,
    capability_probe: Path | str | None = None,
    graph_scene: Path | str | None = None,
    runtime_dependencies: list[Path | str] | None = None,
    measured_toolchain: Path | str | None = None,
    shader_payloads: Path | str | None = None,
    dependency_report: Path | str | None = None,
    python_runtime_zip: Path | str | None = None,
    bundle_version: str = "1.0.0",
    repository: str = "nodewerk",
    repo_root: Path | str | None = None,
    require_head: bool = False,
) -> dict[str, Any]:
    root = Path(repo_root or REPO_ROOT)
    if require_head:
        verify_repository_commit(root, commit)
        if python_runtime_zip is None:
            raise BuildError("production validation bundles require a bundled Python runtime")
    bundle.validate_variant_sources(root)
    output = Path(output_directory)
    if output.exists() and any(output.iterdir()):
        raise BuildError("bundle output directory must be new or empty")
    output.mkdir(parents=True, exist_ok=True)
    experiment_path = root / contract.EXPERIMENT_PATH_BY_ID[experiment_id]
    document = experiment.load_experiment(experiment_path)
    experiment.validate_experiment(document, repo_root=root)
    profile_path = root / contract.PROFILE_PATH_BY_ID[document["variant_profile_id"]]
    profile_bytes = profile_path.read_bytes()
    profile = json.loads(profile_bytes.decode("utf-8"))

    _copy(Path(baseline_executable), output / "bin/common_baseline.exe")
    _copy(Path(variant_executable), output / "bin/optional_variant.exe")
    _copy(Path(fallback_executable), output / "bin/fallback_common.exe")
    if capability_probe is not None:
        _copy(Path(capability_probe), output / "bin/nw11_capability_probe.exe")
    production_records: dict[str, Any] = {}
    if require_head:
        # Only CI production bundles have native measurement artifacts. Unit
        # tests can still exercise the generic manifest logic with test blobs.
        native = Path(baseline_executable).parent
        proof = json.loads((native / "nw11-common-regression.json").read_text(encoding="utf-8"))
        package_hash, manifest_hash = production_binding.verify_proof(proof, commit)
        payloads = production_binding.read_payloads(native / "compiled-specializations")
        bindings = json.loads((native / "nw11-production-binding.json").read_text(encoding="utf-8"))
        if bindings.get("commit") != commit:
            raise BuildError("production binding commit mismatch")
        by_name = {record["file"]: record for record in bindings["artifacts"]}
        variant_kind = "mesh" if experiment_id == contract.EXPERIMENT_MESH_ID else "ray"
        base_kind = "common" if variant_kind == "mesh" else "analytic"
        for role, source, kind in (
            ("common_baseline", baseline_executable, base_kind),
            ("optional_variant", variant_executable, variant_kind),
            ("fallback_common", fallback_executable, base_kind),
        ):
            source = Path(source)
            observed = production_binding.inspect(source.read_bytes(), commit=commit,
                specialization=kind, package_sha256=package_hash,
                manifest_sha256=manifest_hash, payloads=payloads)
            if by_name.get(source.name) != {"file": source.name, **observed}:
                raise BuildError(f"native artifact {source.name} does not match production binding")
            production_records[role] = observed
        _copy(native / "nw11-common-regression.json", output / "manifests/nw11-common-regression.json")
        for name in production_binding.PAYLOADS:
            _copy(native / "compiled-specializations" / name, output / "manifests/compiled-specializations" / name)
    _copy(profile_path, output / "manifests/capability-profile.json")
    _copy(
        root / contract.PROFILE_PATH_BY_ID[contract.COMMON_PROFILE_ID],
        output / "manifests/common-capability-profile.json",
    )
    _copy(experiment_path, output / "manifests/experiment.json")
    _copy(root / contract.FOLLOWUP_MATRIX_PATH, output / "manifests/follow-up-matrix.json")
    _copy(root / contract.SHADER_MANIFEST_PATH, output / "manifests/shader-manifest.json")
    _copy(
        root / contract.CANONICAL_PRODUCTION_CONFIG,
        output / "manifests/canonical-production.json",
    )
    measured_toolchain_document: dict[str, Any] | None = None
    if measured_toolchain is not None:
        measured_toolchain_document = toolchain_identity.verify(
            json.loads(Path(measured_toolchain).read_text(encoding="utf-8"))
        )
        _copy(Path(measured_toolchain), output / "manifests/toolchain-identity.json")
    if shader_payloads is not None:
        payload_document = json.loads(
            Path(shader_payloads).read_text(encoding="utf-8")
        )
        if payload_document.get("format") != "nw11-shader-payloads":
            raise BuildError("shader payload manifest has the wrong format")
        if not payload_document.get("payloads"):
            raise BuildError("shader payload manifest has no payloads")
        _copy(Path(shader_payloads), output / "manifests/shader-payloads.json")
    dependency_document: dict[str, Any] | None = None
    if dependency_report is not None:
        dependency_document = json.loads(
            Path(dependency_report).read_text(encoding="utf-8")
        )
        if dependency_document.get("format") != "nw11-pe-dependencies":
            raise BuildError("dependency report has the wrong format")
        _copy(Path(dependency_report), output / "manifests/pe-dependencies.json")
    if graph_scene is not None:
        _copy(Path(graph_scene), output / "manifests/graph-scene.json")
    _copy(root / LAUNCHER, output / "run-validation.ps1")
    if python_runtime_zip is not None:
        _copy(Path(python_runtime_zip), output / "runtime/python-runtime.zip")
    for name in TOOL_FILES:
        _copy(root / "tools" / name, output / "tools/nw11" / name)
    _copy(root / COLLECTOR, output / "tools/nw11/collect-evidence.py")
    _copy(root / CAPTURE_COMPARATOR, output / "tools/nw11/compare-captures.py")
    for source_relative, destination_relative in SUPPORT_FILES:
        _copy(root / source_relative, output / destination_relative)
    for schema_name in SCHEMA_FILES:
        _copy(
            root / "schemas" / schema_name,
            output / "schemas" / schema_name,
        )

    # Copy every shader that participates in either arm, including shared
    # NW-06 shader payloads.  The bundle preserves the repository-relative
    # path so experiment definitions remain meaningful inside the bundle.
    shader_sources: list[tuple[str, str, bool, str]] = []
    for shader in document["pipeline"]["baseline_shaders"]:
        shader_sources.append(
            (shader["path"], shader["target"], False, contract.COMMON_PROFILE_ID)
        )
    for shader in document["pipeline"]["variant_shaders"]:
        shader_sources.append(
            (
                shader["path"],
                shader["target"],
                True,
                document["variant_profile_id"],
            )
        )
    for key in ("shared_fragment_shader", "shared_vertex_shader"):
        shared = document["pipeline"].get(key)
        if shared:
            shader_sources.append((shared, "", False, contract.COMMON_PROFILE_ID))
    for shared in SHARED_POST_SHADERS:
        shader_sources.append((shared, "cs_6_0", False, contract.COMMON_PROFILE_ID))
    seen_shaders: set[str] = set()
    for source_relative, _target, _optional, _profile_id in shader_sources:
        if source_relative in seen_shaders:
            continue
        seen_shaders.add(source_relative)
        _copy(root / source_relative, output / source_relative)

    dependency_ledger: list[dict[str, Any]] = []
    for dependency in profile.get("dependencies", []):
        dependency_ledger.append(
            {
                "name": dependency["name"],
                "kind": dependency.get("kind", "unknown"),
                "optional": bool(dependency.get("optional", False)),
                "counted": True,
                "in_common_closure": not dependency.get("optional", False),
                "distributed_bytes": int(dependency.get("distributed_bytes", 0)),
            }
        )
    for dependency in runtime_dependencies or []:
        destination = output / "bin" / Path(dependency).name
        _copy(Path(dependency), destination)
        dependency_ledger.append(
            {
                "name": Path(dependency).name,
                "kind": "bundled_runtime",
                "optional": True,
                "counted": True,
                "in_common_closure": False,
                "distributed_bytes": destination.stat().st_size,
            }
        )
    if dependency_document is not None:
        known = {str(item["name"]).casefold() for item in dependency_ledger}
        for executable in dependency_document.get("executables", []):
            for dll in executable.get("dlls", []):
                name = str(dll)
                if name.casefold().endswith(".dll"):
                    name = name[:-4]
                if name.casefold() in known:
                    continue
                known.add(name.casefold())
                dependency_ledger.append(
                    {
                        "name": name,
                        "kind": "windows_inbox",
                        "optional": False,
                        "counted": True,
                        "in_common_closure": True,
                        "distributed_bytes": 0,
                    }
                )

    artifacts = [
        bundle.make_entry(
            output,
            "bin/common_baseline.exe",
            role=bundle.ROLE_COMMON_BASELINE,
            profile_id=contract.COMMON_PROFILE_ID,
            shader_targets=contract.COMMON_SHADER_TARGETS,
            dependencies=[item["name"] for item in dependency_ledger if not item["optional"]],
            metadata=production_records.get("common_baseline"),
        ),
        bundle.make_entry(
            output,
            "bin/optional_variant.exe",
            role=bundle.ROLE_OPTIONAL_VARIANT,
            profile_id=document["variant_profile_id"],
            optional=True,
            shader_targets=[shader["target"] for shader in document["pipeline"]["variant_shaders"]],
            dependencies=[item["name"] for item in dependency_ledger],
            metadata=production_records.get("optional_variant"),
        ),
        bundle.make_entry(
            output,
            "bin/fallback_common.exe",
            role=bundle.ROLE_FALLBACK,
            profile_id=contract.COMMON_PROFILE_ID,
            shader_targets=contract.COMMON_SHADER_TARGETS,
            dependencies=[item["name"] for item in dependency_ledger if not item["optional"]],
            metadata={
                **(production_records.get("fallback_common") or {}),
                "fallback_artifact_id": document["fallback_artifact_id"],
            },
        ),
    ]
    if production_records:
        artifacts.append(bundle.make_entry(output, "manifests/nw11-common-regression.json",
                                           role=bundle.ROLE_READING))
        for name in production_binding.PAYLOADS:
            artifacts.append(bundle.make_entry(output, f"manifests/compiled-specializations/{name}",
                                               role=bundle.ROLE_READING))
    if capability_probe is not None:
        artifacts.append(
            bundle.make_entry(
                output, "bin/nw11_capability_probe.exe", role=bundle.ROLE_TOOL
            )
        )
    for dependency in runtime_dependencies or []:
        artifacts.append(
            bundle.make_entry(
                output,
                f"bin/{Path(dependency).name}",
                role=bundle.ROLE_RUNTIME_DEPENDENCY,
                optional=True,
            )
        )
    artifacts.extend(
        [
            bundle.make_entry(
                output,
                "manifests/capability-profile.json",
                role=bundle.ROLE_CAPABILITY_PROFILE,
                profile_id=profile["profile_id"],
            ),
            bundle.make_entry(
                output,
                "manifests/common-capability-profile.json",
                role=bundle.ROLE_READING,
                profile_id=contract.COMMON_PROFILE_ID,
            ),
            bundle.make_entry(
                output,
                "manifests/experiment.json",
                role=bundle.ROLE_EXPERIMENT_DEFINITION,
            ),
            bundle.make_entry(
                output, "manifests/follow-up-matrix.json", role=bundle.ROLE_READING
            ),
            bundle.make_entry(
                output, "manifests/shader-manifest.json", role=bundle.ROLE_READING
            ),
            bundle.make_entry(
                output, "manifests/canonical-production.json", role=bundle.ROLE_READING
            ),
            bundle.make_entry(output, "run-validation.ps1", role=bundle.ROLE_LAUNCHER),
            *([bundle.make_entry(output, "runtime/python-runtime.zip", role=bundle.ROLE_TOOL)] if python_runtime_zip is not None else []),
        ]
    )
    if measured_toolchain is not None:
        artifacts.append(
            bundle.make_entry(
                output, "manifests/toolchain-identity.json", role=bundle.ROLE_READING
            )
        )
    if shader_payloads is not None:
        artifacts.append(
            bundle.make_entry(
                output, "manifests/shader-payloads.json", role=bundle.ROLE_READING
            )
        )
    if dependency_report is not None:
        artifacts.append(
            bundle.make_entry(
                output, "manifests/pe-dependencies.json", role=bundle.ROLE_READING
            )
        )
    for name in TOOL_FILES:
        artifacts.append(
            bundle.make_entry(output, f"tools/nw11/{name}", role=bundle.ROLE_TOOL)
        )
    artifacts.append(
        bundle.make_entry(output, "tools/nw11/collect-evidence.py", role=bundle.ROLE_TOOL)
    )
    artifacts.append(
        bundle.make_entry(output, "tools/nw11/compare-captures.py", role=bundle.ROLE_TOOL)
    )
    if graph_scene is not None:
        artifacts.append(
            bundle.make_entry(output, "manifests/graph-scene.json", role=bundle.ROLE_READING)
        )
    for source_relative, destination_relative in SUPPORT_FILES:
        artifacts.append(
            bundle.make_entry(output, destination_relative, role=bundle.ROLE_TOOL)
        )
    for schema_name in SCHEMA_FILES:
        artifacts.append(
            bundle.make_entry(
                output, f"schemas/{schema_name}", role=bundle.ROLE_READING
            )
        )
    shader_entries: dict[str, dict[str, Any]] = {}
    for source_relative, target, optional, profile_id in shader_sources:
        if source_relative in shader_entries:
            continue
        entry = bundle.make_entry(
            output,
            source_relative,
            role=bundle.ROLE_SHADER,
            profile_id=profile_id,
            optional=optional,
            shader_targets=[target] if target else [],
        )
        shader_entries[source_relative] = entry
        artifacts.append(entry)

    toolchain = {
        "dxc": profile["toolchain"]["dxc"],
        "msvc": profile["toolchain"]["msvc"],
        "windows_sdk_minimum": profile["toolchain"]["windows_sdk_minimum"],
        "commit": commit,
        "evidence_contract": contract.PERF_CONTRACT_ID,
    }
    if measured_toolchain_document is not None:
        toolchain["measured"] = measured_toolchain_document
    required_capabilities = [
        query["query_id"] for query in profile["queried_capabilities"]
    ] + list(profile["shader_targets"])
    budget = {
        "distribution_size_policy": "gated",
        "complete_distribution_bytes_ceiling": int(
            document["artifact_budget"]["complete_distribution_bytes_ceiling"]
        ),
        "incremental_distribution_bytes_ceiling": int(
            document["artifact_budget"]["incremental_distribution_bytes_ceiling"]
        ),
        "shader_count_increment_ceiling": int(
            document["artifact_budget"]["shader_count_increment_ceiling"]
        ),
    }
    manifest = bundle.build_manifest(
        repository=repository,
        commit=commit,
        experiment_id=experiment_id,
        profile_id=document["variant_profile_id"],
        fallback_artifact_id=document["fallback_artifact_id"],
        artifacts=artifacts,
        capability_profile=profile,
        experiment_definition=document,
        toolchain=toolchain,
        required_capabilities=required_capabilities,
        artifact_budget=budget,
        dependency_ledger=dependency_ledger,
        bundle_version=bundle_version,
    )
    manifest["capability_profile"]["path"] = "manifests/capability-profile.json"
    manifest["capability_profile"]["bytes"] = len(profile_bytes)
    manifest["capability_profile"]["sha256"] = contract.sha256_bytes(profile_bytes)
    manifest["experiment_definition"]["path"] = "manifests/experiment.json"
    manifest["experiment_definition"]["bytes"] = experiment_path.stat().st_size
    manifest["experiment_definition"]["sha256"] = contract.sha256_bytes(
        experiment_path.read_bytes()
    )
    optional_shaders = {
        path: entry for path, entry in shader_entries.items() if entry["optional"]
    }
    common_shaders = {
        path: entry for path, entry in shader_entries.items() if not entry["optional"]
    }
    manifest["shader_inventory"] = {
        "optional": {
            "count": len(optional_shaders),
            "bytes": sum(int(entry["bytes"]) for entry in optional_shaders.values()),
            "paths": sorted(optional_shaders),
        },
        "common": {
            "count": len(common_shaders),
            "bytes": sum(int(entry["bytes"]) for entry in common_shaders.values()),
            "paths": sorted(common_shaders),
        },
        "roles": _role_shader_ids(document),
    }
    if shader_payloads is not None:
        manifest["shader_payloads"] = {
            "path": "manifests/shader-payloads.json",
            "sha256": contract.sha256_bytes(Path(shader_payloads).read_bytes()),
            "payload_count": len(payload_document["payloads"]),
        }
    if dependency_report is not None:
        manifest["dependency_report"] = {
            "path": "manifests/pe-dependencies.json",
            "sha256": contract.sha256_bytes(Path(dependency_report).read_bytes()),
        }
    manifest["canonical_production"] = {
        "path": "manifests/canonical-production.json",
        "sha256": contract.sha256_bytes(
            (root / contract.CANONICAL_PRODUCTION_CONFIG).read_bytes()
        ),
    }
    # Recompute the identity after adding the bundle-relative paths.
    identity = {
        key: manifest[key]
        for key in (
            "repository",
            "commit",
            "experiment_id",
            "profile_id",
            "fallback_artifact_id",
            "bundle_version",
            "artifacts",
            "capability_profile",
            "experiment_definition",
            "toolchain",
            "required_capabilities",
            "dependency_ledger",
            "accounting",
            "shader_inventory",
            "shader_payloads",
            "dependency_report",
            "canonical_production",
        )
        if key in manifest
    }
    manifest["bundle_id"] = contract.digest_value(identity)
    bundle.write_manifest(output, manifest)
    return manifest


INITIALIZATION_SHADER_IDS = (
    "nw06_generate_cs",
    "nw06_instances_cs",
    "nw06_material_cs",
    "nw06_camera_cs",
    "nw06_light_cs",
)

# The NW-07 production post chain is common-profile payload shared by every
# NW-11 artifact; it must be present in the bundle inventory even though only
# the scene stage changes between experiment arms.
SHARED_POST_SHADERS = (
    "shaders/nw06/nw06_bloom_cs.hlsl",
    "shaders/nw06/nw06_tonemap_cs.hlsl",
    "shaders/display_fixture.hlsl",
)


def _shader_id(path: str) -> str:
    return Path(str(path)).stem


def _role_shader_ids(document: dict[str, Any]) -> dict[str, list[str]]:
    pipeline = document["pipeline"]
    init = list(INITIALIZATION_SHADER_IDS)
    post = [_shader_id(path) for path in SHARED_POST_SHADERS]
    baseline = init + post + [
        _shader_id(item["path"]) for item in pipeline["baseline_shaders"]
    ]
    variant = init + post + [
        _shader_id(item["path"]) for item in pipeline["variant_shaders"]
    ]
    for key in ("shared_fragment_shader", "shared_vertex_shader"):
        shared = pipeline.get(key)
        if shared:
            variant.append(_shader_id(shared))
    return {
        "baseline": sorted(set(baseline)),
        "variant": sorted(set(variant)),
        "fallback": sorted(set(baseline)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--baseline-executable", required=True)
    parser.add_argument("--variant-executable", required=True)
    parser.add_argument("--fallback-executable", required=True)
    parser.add_argument("--capability-probe")
    parser.add_argument("--graph-scene")
    parser.add_argument("--runtime-dependency", action="append", default=[])
    parser.add_argument("--toolchain-identity")
    parser.add_argument("--shader-payloads")
    parser.add_argument("--dependency-report")
    parser.add_argument("--python-runtime-zip")
    parser.add_argument("--bundle-version", default="1.0.0")
    parser.add_argument("--repository", default="nodewerk")
    parser.add_argument("--repo-root")
    parser.add_argument(
        "--require-head",
        action="store_true",
        help="fail unless --commit equals the repository working-tree HEAD",
    )
    parser.add_argument("--json-out")
    args = parser.parse_args(argv)
    try:
        manifest = build(
            output_directory=args.output,
            experiment_id=args.experiment_id,
            commit=args.commit,
            baseline_executable=args.baseline_executable,
            variant_executable=args.variant_executable,
            fallback_executable=args.fallback_executable,
            capability_probe=args.capability_probe,
            graph_scene=args.graph_scene,
            runtime_dependencies=args.runtime_dependency,
            measured_toolchain=args.toolchain_identity,
            shader_payloads=args.shader_payloads,
            dependency_report=args.dependency_report,
            python_runtime_zip=args.python_runtime_zip,
            bundle_version=args.bundle_version,
            repository=args.repository,
            repo_root=args.repo_root,
            require_head=args.require_head,
        )
    except (
        BuildError,
        bundle.BundleError,
        experiment.ExperimentError,
        json.JSONDecodeError,
        OSError,
    ) as exc:
        print(f"[NW11-BUILD-ERR] {exc}", file=sys.stderr)
        return 2
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    print(manifest["bundle_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
