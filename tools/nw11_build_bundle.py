#!/usr/bin/env python3
"""Build a deterministic, self-contained NW-11 validation bundle.

CI supplies same-revision executable artifacts.  This tool copies them plus
the repository-owned verifier, launcher, profile, experiment definition, and
optional shader sources into a bundle, records SHA-256 identities for every
file, and writes a versioned bundle manifest.  It never edits evidence or
marks a result verified.
"""

from __future__ import annotations

import argparse
import json
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

TOOL_FILES = (
    "nw11_contract.py",
    "nw11_capability.py",
    "nw11_experiment.py",
    "nw11_bundle.py",
    "nw11_evidence.py",
    "nw11_followup.py",
    "nw11_verify.py",
)
COLLECTOR = "tools/nw11/collect-evidence.py"
LAUNCHER = "tools/nw11/run-validation.ps1"


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
    runtime_dependencies: list[Path | str] | None = None,
    bundle_version: str = "1.0.0",
    repository: str = "nodewerk",
    repo_root: Path | str | None = None,
) -> dict[str, Any]:
    root = Path(repo_root or REPO_ROOT)
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
    _copy(profile_path, output / "manifests/capability-profile.json")
    _copy(experiment_path, output / "manifests/experiment.json")
    _copy(root / contract.FOLLOWUP_MATRIX_PATH, output / "manifests/follow-up-matrix.json")
    _copy(root / LAUNCHER, output / "run-validation.ps1")
    for name in TOOL_FILES:
        _copy(root / "tools" / name, output / "tools/nw11" / name)
    _copy(root / COLLECTOR, output / "tools/nw11/collect-evidence.py")
    for shader in document["pipeline"]["variant_shaders"]:
        _copy(root / shader["path"], output / "shaders" / Path(shader["path"]).name)
    for shader in document["pipeline"]["baseline_shaders"]:
        source = root / shader["path"]
        if source.is_file() and str(shader["path"]).startswith("shaders/nw11/"):
            _copy(source, output / "shaders" / Path(shader["path"]).name)

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

    artifacts = [
        bundle.make_entry(
            output,
            "bin/common_baseline.exe",
            role=bundle.ROLE_COMMON_BASELINE,
            profile_id=contract.COMMON_PROFILE_ID,
            shader_targets=contract.COMMON_SHADER_TARGETS,
            dependencies=[item["name"] for item in dependency_ledger if not item["optional"]],
        ),
        bundle.make_entry(
            output,
            "bin/optional_variant.exe",
            role=bundle.ROLE_OPTIONAL_VARIANT,
            profile_id=document["variant_profile_id"],
            optional=True,
            shader_targets=[shader["target"] for shader in document["pipeline"]["variant_shaders"]],
            dependencies=[item["name"] for item in dependency_ledger],
        ),
        bundle.make_entry(
            output,
            "bin/fallback_common.exe",
            role=bundle.ROLE_FALLBACK,
            profile_id=contract.COMMON_PROFILE_ID,
            shader_targets=contract.COMMON_SHADER_TARGETS,
            dependencies=[item["name"] for item in dependency_ledger if not item["optional"]],
        ),
    ]
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
                "manifests/experiment.json",
                role=bundle.ROLE_EXPERIMENT_DEFINITION,
            ),
            bundle.make_entry(
                output, "manifests/follow-up-matrix.json", role=bundle.ROLE_READING
            ),
            bundle.make_entry(output, "run-validation.ps1", role=bundle.ROLE_LAUNCHER),
        ]
    )
    for name in TOOL_FILES:
        artifacts.append(
            bundle.make_entry(output, f"tools/nw11/{name}", role=bundle.ROLE_TOOL)
        )
    artifacts.append(
        bundle.make_entry(output, "tools/nw11/collect-evidence.py", role=bundle.ROLE_TOOL)
    )
    for shader in document["pipeline"]["variant_shaders"]:
        relative = f"shaders/{Path(shader['path']).name}"
        artifacts.append(
            bundle.make_entry(
                output,
                relative,
                role=bundle.ROLE_SHADER,
                profile_id=document["variant_profile_id"],
                optional=True,
                shader_targets=[shader["target"]],
            )
        )
    for shader in document["pipeline"]["baseline_shaders"]:
        if not str(shader["path"]).startswith("shaders/nw11/"):
            continue
        relative = f"shaders/{Path(shader['path']).name}"
        artifacts.append(
            bundle.make_entry(
                output,
                relative,
                role=bundle.ROLE_SHADER,
                profile_id=contract.COMMON_PROFILE_ID,
                optional=False,
                shader_targets=[shader["target"]],
            )
        )

    toolchain = {
        "dxc": profile["toolchain"]["dxc"],
        "msvc": profile["toolchain"]["msvc"],
        "windows_sdk_minimum": profile["toolchain"]["windows_sdk_minimum"],
        "commit": commit,
        "evidence_contract": contract.PERF_CONTRACT_ID,
    }
    required_capabilities = [
        query["query_id"] for query in profile["queried_capabilities"]
    ] + list(profile["shader_targets"])
    budget = {
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
        )
    }
    manifest["bundle_id"] = contract.digest_value(identity)
    bundle.write_manifest(output, manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--baseline-executable", required=True)
    parser.add_argument("--variant-executable", required=True)
    parser.add_argument("--fallback-executable", required=True)
    parser.add_argument("--capability-probe")
    parser.add_argument("--runtime-dependency", action="append", default=[])
    parser.add_argument("--bundle-version", default="1.0.0")
    parser.add_argument("--repository", default="nodewerk")
    parser.add_argument("--repo-root")
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
            runtime_dependencies=args.runtime_dependency,
            bundle_version=args.bundle_version,
            repository=args.repository,
            repo_root=args.repo_root,
        )
    except (BuildError, bundle.BundleError, experiment.ExperimentError, OSError) as exc:
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
