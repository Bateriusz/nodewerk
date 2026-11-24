#!/usr/bin/env python3
"""Deterministic, self-contained NW-11 validation bundles.

A bundle binds a same-revision common/baseline executable, an optional
variant executable, a separately identifiable common-profile fallback
artifact, every runtime dependency, the exact capability/experiment
manifests, and SHA-256 identities.  Verification is fail-closed: missing,
extra, stale, or misclassified bytes reject the bundle.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from nw11_contract import (  # type: ignore[import-not-found]
    BASE_GRAPH,
    BASE_GRAPH_SHA256,
    COMMON_PROFILE_ID,
    IMPLEMENTATION_BASE,
    TASK,
    canonical_json_bytes,
    digest_value,
    sha256_bytes,
)

BUNDLE_FORMAT = "nw11-validation-bundle-manifest"
BUNDLE_VERSION = 1
MANIFEST_NAME = "bundle-manifest.json"

ROLE_COMMON_BASELINE = "common_baseline"
ROLE_OPTIONAL_VARIANT = "optional_variant"
ROLE_FALLBACK = "fallback_common"
ROLE_RUNTIME_DEPENDENCY = "runtime_dependency"
ROLE_CAPABILITY_PROFILE = "capability_profile"
ROLE_EXPERIMENT_DEFINITION = "experiment_definition"
ROLE_TOOL = "verification_tool"
ROLE_LAUNCHER = "launcher"
ROLE_SHADER = "shader"
ROLE_READING = "reading"

KNOWN_ROLES = (
    ROLE_COMMON_BASELINE,
    ROLE_OPTIONAL_VARIANT,
    ROLE_FALLBACK,
    ROLE_RUNTIME_DEPENDENCY,
    ROLE_CAPABILITY_PROFILE,
    ROLE_EXPERIMENT_DEFINITION,
    ROLE_TOOL,
    ROLE_LAUNCHER,
    ROLE_SHADER,
    ROLE_READING,
)

DISTRIBUTED_ROLES = {
    ROLE_COMMON_BASELINE,
    ROLE_OPTIONAL_VARIANT,
    ROLE_FALLBACK,
    ROLE_RUNTIME_DEPENDENCY,
    ROLE_SHADER,
}

# Any dependency whose name matches these tokens is optional; it may never be
# present in a common/fallback artifact closure.
OPTIONAL_DEPENDENCY_TOKENS = (
    "dlss",
    "nvngx",
    "rtx",
    "omniverse",
    "nrc",
    "dmm",
    "tensorrt",
    "cuda",
    "slang",
    "dxcompiler",
    "agility",
    "nvidia",
)


class BundleError(ValueError):
    """Raised when a bundle is malformed, stale, incomplete, or misclassified."""


def validate_variant_sources(repo_root: Path | str) -> dict[str, Any]:
    """Validate optional shader synchronization and resource-lifetime claims.

    This is a static contract check.  It does not replace physical execution;
    it fails closed if an optional shader loses the barrier, ray-query commit
    loop, or lifetime declaration that the bundle evidence relies on.
    """
    root = Path(repo_root)
    manifest_path = root / "shaders" / "nw11" / "manifest.json"
    if not manifest_path.is_file():
        raise BundleError("NW-11 shader manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "nw11-shader-manifest":
        raise BundleError("NW-11 shader manifest has the wrong format")
    if manifest.get("task") != TASK:
        raise BundleError("NW-11 shader manifest has the wrong task")
    if manifest.get("common_profile_excluded") is not True:
        raise BundleError("NW-11 shaders must be excluded from common profiles")
    if manifest.get("optional_sdk_dependencies") != []:
        raise BundleError("NW-11 shader manifest declares an optional SDK dependency")
    entries = manifest.get("shaders")
    if not isinstance(entries, list) or not entries:
        raise BundleError("NW-11 shader manifest has no shaders")
    checked = []
    for entry in entries:
        source_path = _mapping(entry, "shader entry").get("source_path")
        if not isinstance(source_path, str) or not source_path.startswith("shaders/nw11/"):
            raise BundleError("optional shader source must live under shaders/nw11/")
        source = root / source_path
        if not source.is_file():
            raise BundleError(f"optional shader source is missing: {source_path}")
        text = source.read_text(encoding="utf-8")
        target = str(entry.get("target_profile", ""))
        if target.startswith("ms_"):
            if "SetMeshOutputCounts" not in text or "GroupMemoryBarrierWithGroupSync" not in text:
                raise BundleError(
                    f"mesh shader '{source_path}' is missing a required output-count or group barrier"
                )
            if "NwVertices" not in text or "NwVertices.Load" not in text:
                raise BundleError(
                    f"mesh shader '{source_path}' must consume the graph-generated "
                    "vertex buffer instead of reconstructing hard-coded geometry"
                )
            normalized = re.sub(r"\s+", "", text)
            if "numthreads(NW_VERTS_PER_MESHLET,1,1)" not in normalized:
                raise BundleError(
                    f"mesh shader '{source_path}' must launch one thread per local "
                    "patch vertex so every mesh output vertex is written"
                )
            if "SetMeshOutputCounts(NW_VERTS_PER_MESHLET,NW_PRIMS_PER_MESHLET)" not in normalized:
                raise BundleError(
                    f"mesh shader '{source_path}' vertex output count must match "
                    "the emitted local patch size"
                )
            if "vertex_index*32u" not in normalized or "ByteAddressBufferNwVertices" not in normalized:
                raise BundleError(f"mesh shader '{source_path}' must use 32-byte raw vertex records")
            if "NwIndices.Load((quad*6u+corner)*4u)" not in normalized or "ByteAddressBufferNwIndices" not in normalized:
                raise BundleError(f"mesh shader '{source_path}' must use byte-addressed generated indices")
        if "RayQuery<" in text:
            for token in ("TraceRayInline", "Proceed", "CommittedStatus"):
                if token not in text:
                    raise BundleError(
                        f"ray-query shader '{source_path}' is missing '{token}'"
                    )
        lifetime = entry.get("resource_lifetime") or {}
        if not isinstance(lifetime, Mapping):
            raise BundleError(f"shader '{source_path}' has invalid resource_lifetime")
        checked.append(
            {
                "shader_id": entry.get("shader_id"),
                "target_profile": target,
                "optional": bool(entry.get("optional")),
                "synchronization": entry.get("synchronization") or {},
            }
        )
    return {
        "status": "verified",
        "common_profile_excluded": True,
        "shaders": checked,
    }


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BundleError(f"{label} must be a JSON object")
    return value


def _safe_relative(path: str) -> str:
    if not isinstance(path, str) or not path.strip():
        raise BundleError("artifact path must be a non-empty string")
    candidate = PurePosixPath(path.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts:
        raise BundleError(f"artifact path must be bundle-relative: {path}")
    return candidate.as_posix()


def _schema_validator():
    """Return the repository-owned zero-dependency schema validator.

    The validator is copied into every bundle so verification never depends
    on a repository checkout or an installed third-party package.
    """
    try:
        import schema_validator as validator  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - import guard
        raise BundleError(
            f"the bundle schema validator is unavailable: {exc}"
        ) from exc
    return validator


def _validate_with_schema(
    bundle_root: Path, relative_schema: str, document: Mapping[str, Any], label: str
) -> None:
    schema_path = bundle_root / relative_schema
    if not schema_path.is_file():
        raise BundleError(f"bundle schema is missing: {relative_schema}")
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BundleError(f"bundle schema '{relative_schema}' is malformed: {exc}") from exc
    try:
        _schema_validator().validate_instance(document, schema, schema)
    except Exception as exc:
        raise BundleError(f"bundle {label} failed schema validation: {exc}") from exc


def make_entry(
    root: Path | str,
    relative_path: str,
    *,
    role: str,
    profile_id: str | None = None,
    optional: bool = False,
    shader_targets: Iterable[str] = (),
    dependencies: Iterable[str] = (),
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if role not in KNOWN_ROLES:
        raise BundleError(f"unknown artifact role '{role}'")
    relative = _safe_relative(relative_path)
    path = Path(root) / relative
    if not path.is_file():
        raise BundleError(f"bundle artifact is missing: {relative}")
    data = path.read_bytes()
    distributed = role in DISTRIBUTED_ROLES
    return {
        "path": relative,
        "role": role,
        "sha256": sha256_bytes(data),
        "bytes": len(data),
        "distributed_bytes": len(data) if distributed else 0,
        "profile_id": profile_id,
        "optional": bool(optional),
        "shader_targets": sorted(set(str(item) for item in shader_targets)),
        "dependencies": sorted(set(str(item) for item in dependencies)),
        "metadata": dict(metadata or {}),
    }


def _accounting(
    artifacts: list[dict[str, Any]],
    *,
    artifact_budget: Mapping[str, Any],
    dependency_ledger: list[dict[str, Any]],
) -> dict[str, Any]:
    common = sum(
        item["distributed_bytes"]
        for item in artifacts
        if item["role"] == ROLE_COMMON_BASELINE
    )
    fallback = sum(
        item["distributed_bytes"]
        for item in artifacts
        if item["role"] == ROLE_FALLBACK
    )
    optional = sum(
        item["distributed_bytes"]
        for item in artifacts
        if item["role"] == ROLE_OPTIONAL_VARIANT
    )
    optional_shaders = sum(
        item["distributed_bytes"]
        for item in artifacts
        if item["role"] == ROLE_SHADER and item["optional"]
    )
    optional_dependencies = sum(
        int(item.get("distributed_bytes", 0))
        for item in dependency_ledger
        if item.get("optional")
    )
    common_shaders = sum(
        item["distributed_bytes"]
        for item in artifacts
        if item["role"] == ROLE_SHADER and not item.get("optional")
    )
    # The variant distribution is the optional executable plus every optional
    # shader and dependency byte that must ship with it.  Common/fallback
    # shaders are part of the common distribution so incremental accounting
    # reflects the actual payload added by the optional profile.
    baseline_distribution = common + common_shaders
    # Include the shared/common shader payloads the variant also needs so the
    # reported variant and incremental distributions reflect every shipped
    # byte.  Common shader bytes cancel in the incremental difference.
    variant_distribution = (
        optional + optional_shaders + common_shaders + optional_dependencies
    )
    incremental = variant_distribution - baseline_distribution
    fallback_distribution = fallback + common_shaders
    complete = max(baseline_distribution, variant_distribution, fallback_distribution)
    return {
        "distribution_size_policy": "report_only",
        "common_distribution_bytes": common,
        "baseline_distribution_bytes": baseline_distribution,
        "fallback_distribution_bytes": fallback_distribution,
        "optional_distribution_bytes": optional,
        "optional_shader_bytes": optional_shaders,
        "common_shader_bytes": common_shaders,
        "optional_dependency_bytes": optional_dependencies,
        "variant_distribution_bytes": variant_distribution,
        "optional_shader_count": sum(
            1
            for item in artifacts
            if item["role"] == ROLE_SHADER and item.get("optional")
        ),
        "common_shader_count": sum(
            1
            for item in artifacts
            if item["role"] == ROLE_SHADER and not item.get("optional")
        ),
        "incremental_distribution_bytes": incremental,
        "complete_distribution_bytes": complete,
        "total_bundle_bytes": sum(item["bytes"] for item in artifacts),
        "artifact_budget": dict(artifact_budget),
    }


def build_manifest(
    *,
    repository: str,
    commit: str,
    experiment_id: str,
    profile_id: str,
    fallback_artifact_id: str,
    artifacts: list[dict[str, Any]],
    capability_profile: Mapping[str, Any],
    experiment_definition: Mapping[str, Any],
    toolchain: Mapping[str, Any],
    required_capabilities: Iterable[str],
    artifact_budget: Mapping[str, Any],
    dependency_ledger: list[dict[str, Any]],
    bundle_version: str = "1.0.0",
) -> dict[str, Any]:
    if not isinstance(repository, str) or not repository.strip():
        raise BundleError("repository identity is required")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise BundleError("bundle commit must be a full 40-character git SHA")
    if not isinstance(bundle_version, str) or re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+", bundle_version
    ) is None:
        raise BundleError("bundle_version must be a semantic version")

    normalized: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for entry in artifacts:
        item = _mapping(entry, "artifact entry")
        path = _safe_relative(str(item.get("path", "")))
        if path in seen_paths:
            raise BundleError(f"duplicate artifact path '{path}'")
        seen_paths.add(path)
        if item.get("role") not in KNOWN_ROLES:
            raise BundleError(f"artifact '{path}' has unknown role")
        normalized.append(dict(item))
    normalized.sort(key=lambda item: item["path"])

    if not any(item["role"] == ROLE_COMMON_BASELINE for item in normalized):
        raise BundleError("bundle has no common baseline executable")
    if not any(item["role"] == ROLE_OPTIONAL_VARIANT for item in normalized):
        raise BundleError("bundle has no optional variant executable")
    if not any(item["role"] == ROLE_FALLBACK for item in normalized):
        raise BundleError("bundle has no separately exported common fallback artifact")

    _assert_optional_closure_separation(normalized, dependency_ledger)
    accounting = _accounting(
        normalized,
        artifact_budget=artifact_budget,
        dependency_ledger=dependency_ledger,
    )

    identity = {
        "repository": repository,
        "commit": commit,
        "experiment_id": experiment_id,
        "profile_id": profile_id,
        "fallback_artifact_id": fallback_artifact_id,
        "bundle_version": bundle_version,
        "artifacts": normalized,
        "capability_profile": {
            "profile_id": capability_profile.get("profile_id"),
            "requirements_digest": capability_profile.get("requirements_digest"),
            "sha256": digest_value(capability_profile),
        },
        "experiment_definition": {
            "experiment_id": experiment_definition.get("experiment_id"),
            "sha256": digest_value(experiment_definition),
        },
        "toolchain": dict(toolchain),
        "required_capabilities": sorted(set(str(item) for item in required_capabilities)),
        "dependency_ledger": sorted(
            (dict(item) for item in dependency_ledger),
            key=lambda item: str(item.get("name", "")),
        ),
        "accounting": accounting,
    }
    manifest = {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "task": TASK,
        "implementation_base": IMPLEMENTATION_BASE,
        "bundle_id": digest_value(identity),
        "bundle_version": bundle_version,
        "verification": {
            "verifier": "tools/nw11/nw11_verify.py",
            "launcher": "run-validation.ps1",
            "launcher_verifies_before_measurement": True,
            "evidence_binds_commit_and_hashes": True,
            "operator_supplied_values_allowed": False,
        },
        "policy": (
            "The bundle is deterministic and self-contained. The launcher verifies "
            "identities and hashes before measuring, records adapter/driver/OS and "
            "queried D3D12 capabilities, executes common/baseline and eligible "
            "variants under identical declared settings, and runs the repository-"
            "owned verifier before reporting verified evidence. Capability absence "
            "is unsupported, never a silent effect substitution. A verified bundle "
            "reports distribution size without imposing a byte ceiling; it is not an adopted export."
        ),
        **identity,
    }
    return manifest


def _optional_dependency(name: str) -> bool:
    lowered = name.casefold()
    return any(token in lowered for token in OPTIONAL_DEPENDENCY_TOKENS)


def _assert_optional_closure_separation(
    artifacts: list[dict[str, Any]], dependency_ledger: list[dict[str, Any]]
) -> None:
    optional_shader_paths = {
        item["path"]
        for item in artifacts
        if item["role"] == ROLE_SHADER and item.get("optional")
    }
    for item in artifacts:
        if item["role"] not in {ROLE_COMMON_BASELINE, ROLE_FALLBACK}:
            continue
        if item.get("profile_id") != COMMON_PROFILE_ID:
            raise BundleError(
                f"common/fallback artifact '{item['path']}' must use the common profile"
            )
        if item.get("optional"):
            raise BundleError(
                f"common/fallback artifact '{item['path']}' must not be optional"
            )
        targets = set(item.get("shader_targets") or [])
        if any(target.startswith(("as_", "ms_", "lib_")) for target in targets):
            raise BundleError(
                f"common/fallback artifact '{item['path']}' includes optional shader targets"
            )
        if any(path in item.get("via", []) for path in optional_shader_paths):
            raise BundleError(
                f"common/fallback artifact '{item['path']}' references an optional shader"
            )
        for dependency in item.get("dependencies") or []:
            if _optional_dependency(str(dependency)):
                raise BundleError(
                    f"common/fallback artifact '{item['path']}' depends on optional component "
                    f"'{dependency}'"
                )
    for dependency in dependency_ledger:
        if dependency.get("optional") and dependency.get("in_common_closure") is True:
            raise BundleError(
                f"optional dependency '{dependency.get('name')}' is part of the common closure"
            )
        if dependency.get("optional") and dependency.get("counted") is not True:
            raise BundleError(
                f"optional dependency '{dependency.get('name')}' must be fully counted"
            )


def write_manifest(root: Path | str, manifest: Mapping[str, Any]) -> Path:
    target = Path(root) / MANIFEST_NAME
    target.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return target


def load_manifest(root: Path | str) -> dict[str, Any]:
    path = Path(root) / MANIFEST_NAME
    if not path.is_file():
        raise BundleError(f"bundle manifest is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BundleError(f"bundle manifest is malformed: {exc}") from exc
    return verify_manifest_structure(value)


def verify_manifest_structure(record: Mapping[str, Any]) -> dict[str, Any]:
    manifest = _mapping(record, "bundle manifest")
    if manifest.get("format") != BUNDLE_FORMAT:
        raise BundleError("bundle manifest has the wrong format")
    if manifest.get("version") != BUNDLE_VERSION:
        raise BundleError("bundle manifest version is not 1")
    if manifest.get("task") != TASK:
        raise BundleError("bundle manifest task is not NW-11")
    if manifest.get("implementation_base") != IMPLEMENTATION_BASE:
        raise BundleError("bundle is not bound to the NW-11 implementation base")
    commit = manifest.get("commit")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise BundleError("bundle commit is malformed")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise BundleError("bundle artifacts must be a non-empty array")
    if not any(a.get("role") == ROLE_COMMON_BASELINE for a in artifacts):
        raise BundleError("bundle has no common baseline artifact")
    if not any(a.get("role") == ROLE_OPTIONAL_VARIANT for a in artifacts):
        raise BundleError("bundle has no optional variant artifact")
    if not any(a.get("role") == ROLE_FALLBACK for a in artifacts):
        raise BundleError("bundle has no fallback artifact")
    verification = _mapping(manifest.get("verification"), "verification")
    if verification.get("operator_supplied_values_allowed") is not False:
        raise BundleError("bundle permits operator-supplied measured values")

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
    if manifest.get("bundle_id") != digest_value(identity):
        raise BundleError("bundle_id does not match the manifest identity")
    return dict(manifest)


def verify_bundle(
    root: Path | str,
    *,
    expected_commit: str | None = None,
) -> dict[str, Any]:
    bundle_root = Path(root).resolve()
    manifest = load_manifest(bundle_root)
    _validate_with_schema(
        bundle_root,
        "schemas/nw11-bundle-manifest.schema.json",
        manifest,
        "bundle manifest",
    )
    if expected_commit is not None and manifest["commit"] != expected_commit:
        raise BundleError(
            "bundle commit does not match the expected tested commit; evidence is stale"
        )

    artifact_paths: set[str] = set()
    for artifact in manifest["artifacts"]:
        relative = _safe_relative(str(artifact.get("path", "")))
        artifact_paths.add(relative)
        path = bundle_root / relative
        if not path.is_file():
            raise BundleError(f"bundle artifact is missing: {relative}")
        data = path.read_bytes()
        if len(data) != int(artifact.get("bytes", -1)):
            raise BundleError(f"bundle artifact size changed: {relative}")
        expected_distributed = len(data) if artifact["role"] in DISTRIBUTED_ROLES else 0
        if artifact.get("distributed_bytes") != expected_distributed:
            raise BundleError(f"bundle distribution accounting changed: {relative}")
        if sha256_bytes(data) != artifact.get("sha256"):
            raise BundleError(f"bundle artifact hash changed: {relative}")

    proof_path = bundle_root / "manifests/nw11-common-regression.json"
    measured = [item for item in manifest["artifacts"] if item["role"] in
                {ROLE_COMMON_BASELINE, ROLE_OPTIONAL_VARIANT, ROLE_FALLBACK}]
    if proof_path.is_file() or any(item.get("metadata", {}).get("specialization") for item in measured):
        try:
            import nw11_production_binding as binding
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            package_hash, manifest_hash = binding.verify_proof(proof, manifest["commit"])
            payloads = binding.read_payloads(bundle_root / "manifests/compiled-specializations")
            ray = manifest["experiment_id"] == "nw11.ray-query-vs-analytic-reflection.v1"
            for item in measured:
                specialization = ("ray" if ray else "mesh") if item["role"] == ROLE_OPTIONAL_VARIANT else ("analytic" if ray else "common")
                observed = binding.inspect((bundle_root / item["path"]).read_bytes(),
                    commit=manifest["commit"], specialization=specialization,
                    package_sha256=package_hash, manifest_sha256=manifest_hash,
                    payloads=payloads)
                if observed != item.get("metadata"):
                    raise ValueError("recorded native binding differs from executable bytes")
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise BundleError(f"production binding verification failed: {exc}") from exc

    for key, role in (
        ("capability_profile", ROLE_CAPABILITY_PROFILE),
        ("experiment_definition", ROLE_EXPERIMENT_DEFINITION),
    ):
        identity = _mapping(manifest.get(key), key)
        artifact = next(
            (item for item in manifest["artifacts"] if item["role"] == role), None
        )
        if artifact is None:
            raise BundleError(f"bundle has no {role} artifact")
        if identity.get("path") != artifact["path"] or identity.get("sha256") != artifact[
            "sha256"
        ]:
            raise BundleError(f"bundle {key} identity disagrees with its artifact entry")

    try:
        import nw11_capability as capability
        import nw11_experiment as experiment
    except Exception as exc:  # pragma: no cover - import guard
        raise BundleError(f"cannot load NW-11 contract modules: {exc}") from exc
    profile_path = bundle_root / manifest["capability_profile"]["path"]
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        capability.validate_profile(profile)
        _validate_with_schema(
            bundle_root,
            "schemas/nw11-capability-profile.schema.json",
            profile,
            "capability profile",
        )
    except Exception as exc:
        raise BundleError(f"bundle capability profile is invalid: {exc}") from exc
    if manifest["capability_profile"].get("requirements_digest") != profile.get(
        "requirements_digest"
    ):
        raise BundleError("bundle capability profile requirements digest is stale")
    if manifest["capability_profile"].get("profile_id") != profile.get("profile_id"):
        raise BundleError("bundle capability profile identity is stale")
    if manifest.get("profile_id") != profile.get("profile_id"):
        raise BundleError("bundle profile identity disagrees with the profile artifact")
    common_profile_path = bundle_root / "manifests" / "common-capability-profile.json"
    if not common_profile_path.is_file():
        raise BundleError("bundle common capability profile is missing")
    try:
        common_profile = json.loads(common_profile_path.read_text(encoding="utf-8"))
        capability.validate_profile(common_profile)
        _validate_with_schema(
            bundle_root,
            "schemas/nw11-capability-profile.schema.json",
            common_profile,
            "common capability profile",
        )
    except Exception as exc:
        raise BundleError(f"bundle common capability profile is invalid: {exc}") from exc
    if common_profile.get("profile_id") != COMMON_PROFILE_ID:
        raise BundleError("bundle common capability profile has the wrong identity")
    experiment_path = bundle_root / manifest["experiment_definition"]["path"]
    try:
        experiment_document = json.loads(experiment_path.read_text(encoding="utf-8"))
        experiment.validate_experiment(experiment_document)
        _validate_with_schema(
            bundle_root,
            "schemas/nw11-experiment.schema.json",
            experiment_document,
            "experiment definition",
        )
    except Exception as exc:
        raise BundleError(f"bundle experiment definition is invalid: {exc}") from exc
    if experiment_document.get("experiment_id") != manifest.get("experiment_id"):
        raise BundleError("bundle experiment identity disagrees with the definition")
    if experiment_document.get("variant_profile_id") != manifest.get("profile_id"):
        raise BundleError("bundle profile identity disagrees with the experiment definition")
    if "shader_payloads" in manifest:
        record = _mapping(manifest["shader_payloads"], "shader_payloads")
        payload_path = bundle_root / str(record.get("path", ""))
        if not payload_path.is_file():
            raise BundleError("bundle shader payload manifest is missing")
        payload_bytes = payload_path.read_bytes()
        if sha256_bytes(payload_bytes) != record.get("sha256"):
            raise BundleError("bundle shader payload manifest hash changed")
        try:
            payload_document = json.loads(payload_bytes.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise BundleError(f"bundle shader payload manifest is malformed: {exc}") from exc
        if payload_document.get("format") != "nw11-shader-payloads" or not payload_document.get(
            "payloads"
        ):
            raise BundleError("bundle shader payload manifest is malformed")
    if "dependency_report" in manifest:
        record = _mapping(manifest["dependency_report"], "dependency_report")
        report_path = bundle_root / str(record.get("path", ""))
        if not report_path.is_file():
            raise BundleError("bundle dependency report is missing")
        report_bytes = report_path.read_bytes()
        if sha256_bytes(report_bytes) != record.get("sha256"):
            raise BundleError("bundle dependency report hash changed")
        try:
            report_document = json.loads(report_bytes.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise BundleError(f"bundle dependency report is malformed: {exc}") from exc
        if report_document.get("format") != "nw11-pe-dependencies":
            raise BundleError("bundle dependency report has the wrong format")
    canonical = _mapping(
        manifest.get("canonical_production"), "canonical_production"
    )
    canonical_path = bundle_root / str(canonical.get("path", ""))
    if not canonical_path.is_file():
        raise BundleError("bundle canonical production contract is missing")
    canonical_bytes = canonical_path.read_bytes()
    if sha256_bytes(canonical_bytes) != canonical.get("sha256"):
        raise BundleError("bundle canonical production contract hash changed")
    try:
        canonical_document = json.loads(canonical_bytes.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise BundleError(
            f"bundle canonical production contract is malformed: {exc}"
        ) from exc
    if canonical_document.get("format") != "nw07-canonical-production":
        raise BundleError("bundle canonical production contract has the wrong format")
    if canonical_document.get("graph_sha256") != BASE_GRAPH_SHA256:
        raise BundleError(
            "bundle canonical production contract does not match the frozen graph"
        )
    measured_toolchain = (
        _mapping(manifest.get("toolchain"), "toolchain").get("measured")
    )
    if measured_toolchain is not None:
        try:
            import nw11_toolchain_identity as toolchain_identity

            toolchain_identity.verify(dict(measured_toolchain))
        except Exception as exc:
            raise BundleError(f"bundle measured toolchain identity is invalid: {exc}") from exc
    graph_scene_path = bundle_root / "manifests" / "graph-scene.json"
    if graph_scene_path.is_file():
        try:
            graph_scene = json.loads(graph_scene_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BundleError(f"bundle graph-scene manifest is malformed: {exc}") from exc
        if (
            graph_scene.get("graph_path") != BASE_GRAPH
            or graph_scene.get("graph_sha256") != BASE_GRAPH_SHA256
        ):
            raise BundleError(
                "bundle graph-scene manifest does not match the frozen NW-07 graph"
            )
    recorded_budget = _mapping(
        _mapping(manifest.get("accounting"), "accounting").get("artifact_budget"),
        "accounting.artifact_budget",
    )
    experiment_budget = _mapping(
        experiment_document.get("artifact_budget"), "experiment.artifact_budget"
    )
    if recorded_budget != {"distribution_size_policy": "report_only",
                           "shader_count_increment_ceiling": experiment_budget["shader_count_increment_ceiling"]}:
        raise BundleError("bundle accounting policy does not match experiment")

    # Fail closed on unrecorded payload files anywhere in the bundle.  Raw
    # evidence is written outside the immutable bundle root by the launcher.
    for path in sorted(bundle_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(bundle_root).as_posix()
        if relative == MANIFEST_NAME:
            continue
        if relative.startswith("tools/nw11/__pycache__/") and relative.endswith(".pyc"):
            # Interpreter cache generated by running the bundled verifier is
            # not distributed payload.  It is the only tolerated unrecorded
            # file class; the launcher also disables bytecode writing.
            continue
        if relative not in artifact_paths:
            raise BundleError(
                f"bundle contains an unrecorded payload file: {relative}; "
                "strip unused optional implementations and shaders"
            )

    _assert_optional_closure_separation(
        list(manifest["artifacts"]), list(manifest.get("dependency_ledger") or [])
    )
    accounting = _accounting(
        list(manifest["artifacts"]),
        artifact_budget=_mapping(manifest["accounting"], "accounting")[
            "artifact_budget"
        ],
        dependency_ledger=list(manifest.get("dependency_ledger") or []),
    )
    if accounting != manifest["accounting"]:
        raise BundleError("bundle accounting does not match recorded artifact bytes")
    return {
        "status": "verified",
        "bundle_id": manifest["bundle_id"],
        "commit": manifest["commit"],
        "experiment_id": manifest["experiment_id"],
        "profile_id": manifest["profile_id"],
        "fallback_artifact_id": manifest["fallback_artifact_id"],
        "accounting": accounting,
        "manifest": manifest,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify an NW-11 validation bundle.")
    parser.add_argument("--bundle-root", required=True)
    parser.add_argument("--expected-commit")
    parser.add_argument("--json-out")
    args = parser.parse_args(argv)
    try:
        result = verify_bundle(args.bundle_root, expected_commit=args.expected_commit)
    except BundleError as exc:
        print(f"[NW11-BUNDLE-ERR] {exc}")
        return 2
    payload = {key: value for key, value in result.items() if key != "manifest"}
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8", newline="\n")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
