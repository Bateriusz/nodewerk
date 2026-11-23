#!/usr/bin/env python3
"""Build schema-valid NW-10 Windows compact feasibility evidence.

Consumes the Windows workflow's measured size outcome plus the decoder-off
proof report and the restricted-intro report.  Both the failure/continue and
the true at-most-65536-byte success branches are representable.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw10_contract as contract  # noqa: E402
import nw07_clean_system as clean_system_contract  # noqa: E402
import nw07_contract as common_contract  # noqa: E402
import nw07_evidence  # noqa: E402
import nw07_export  # noqa: E402
import nw05_export  # noqa: E402
import nw10_compact as compact  # noqa: E402
import nw10_perf_preservation  # noqa: E402
import nwp_package as nwp  # noqa: E402
from schema_validator import (  # noqa: E402
    SchemaValidationError,
    validate_file_against_schema,
    validate_instance,
    validate_semantic_invariants,
)

FORMAT = "nw10-windows-compact-feasibility"
WINDOWS_EXIT_MIN = -(2**31)
WINDOWS_EXIT_MAX = 2**31 - 1
PERF_REFERENCE_PATH = REPO_ROOT / "benchmarks" / "nw07" / "perf-ref-v1.json"
DEPENDENCY_INVENTORY_PATH = (
    REPO_ROOT / "benchmarks" / "dependency-inventory.v1.json"
)


def _windows_exit_code(value: Any, *, default: int | None = None) -> int | None:
    if value is None:
        return default
    code = int(value)
    if code < WINDOWS_EXIT_MIN or code > WINDOWS_EXIT_MAX:
        raise ValueError(f"Windows process exit code is outside signed 32-bit range: {code}")
    return code


def _windows_exit_hex(code: int | None) -> str | None:
    if code is None:
        return None
    return f"0x{code & 0xFFFFFFFF:08X}"


def _canonical_dependencies(report: Mapping[str, Any]) -> list[str]:
    ledgers = report.get("ledgers")
    canonical = ledgers.get("canonical") if isinstance(ledgers, Mapping) else None
    rows = canonical.get("dependencies") if isinstance(canonical, Mapping) else None
    if not isinstance(rows, list):
        return []
    names = {
        str(row.get("name", "")).strip().lower()
        for row in rows
        if isinstance(row, Mapping) and str(row.get("name", "")).strip()
    }
    return sorted(names)


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def _repo_relative_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(
            "prior valid ledger must be inside the repository for public provenance"
        ) from exc


def _validate_prior_valid_ledger(
    ledger: Mapping[str, Any], *, provenance_path: str
) -> tuple[Mapping[str, Any], Mapping[str, Any], str | None]:
    if ledger.get("format") != "nw10-windows-pe-ledger":
        raise ValueError("prior valid ledger has an unexpected format")
    if ledger.get("version") != 1 or ledger.get("task") != "NW-10":
        raise ValueError("prior valid ledger has an unexpected version or task")
    if not provenance_path or provenance_path.startswith("/") or re.match(
        r"^[A-Za-z]:", provenance_path
    ):
        raise ValueError("prior valid ledger provenance path must be repository-relative")
    final = ledger.get("final_artifact")
    conclusion = ledger.get("conclusion")
    if not isinstance(final, Mapping) or not isinstance(conclusion, Mapping):
        raise ValueError("prior valid ledger is structurally incomplete")
    if final.get("runtime_status") != "validated_runnable":
        raise ValueError("prior valid ledger does not contain a runnable artifact")
    if conclusion.get("byte_ledger_complete") is not True:
        raise ValueError("prior valid ledger does not contain a complete byte ledger")
    executable_sha = str(final.get("executable_sha256", "")).lower()
    if re.fullmatch(r"[0-9a-f]{64}", executable_sha) is None:
        raise ValueError("prior valid ledger executable SHA-256 is malformed")
    for field in ("package_sha256", "manifest_sha256"):
        value = final.get(field)
        if value is not None and re.fullmatch(r"[0-9a-f]{64}", str(value).lower()) is None:
            raise ValueError(f"prior valid ledger {field} is malformed")
    if int(final.get("executable_bytes", 0)) <= 0:
        raise ValueError("prior valid ledger executable size is invalid")
    source = ledger.get("source")
    prior_head: str | None = None
    if isinstance(source, Mapping) and source.get("head_sha") is not None:
        prior_head = str(source.get("head_sha")).lower()
        if re.fullmatch(r"[0-9a-f]{40}", prior_head) is None:
            raise ValueError("prior valid ledger source head is malformed")
    return final, conclusion, prior_head


def _normalize_artifact_digest(value: str) -> str:
    digest = str(value).strip().lower()
    if not digest:
        return ""
    if digest.startswith("sha256:"):
        hex_digest = digest[len("sha256:"):]
    else:
        hex_digest = digest
    if len(hex_digest) != 64 or any(
        character not in "0123456789abcdef" for character in hex_digest
    ):
        raise ValueError(
            "artifact digest must be a SHA-256 digest as 64 hex characters "
            "or sha256:<64 hex characters>"
        )
    return "sha256:" + hex_digest


def _proof_artifact(report: Mapping[str, Any]) -> Mapping[str, Any]:
    artifact = report.get("canonical_artifact")
    if not isinstance(artifact, Mapping):
        raise ValueError("proof report has no canonical_artifact")
    return artifact


def _intro_artifacts(report: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    canonical = report.get("canonical_artifact")
    compact = report.get("compact_artifact")
    if not isinstance(canonical, Mapping) or not isinstance(compact, Mapping):
        raise ValueError("intro report lacks canonical or compact artifacts")
    return canonical, compact


def _status(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("status") or "")
    return ""


def _is_finite_nonnegative(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0.0
    )


def _is_strict_under(value: Any, limit: float) -> bool:
    return _is_finite_nonnegative(value) and float(value) < limit


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _evidence_bytes(value: Any) -> bytes:
    if isinstance(value, Mapping):
        value = value.get("executable", value.get("path"))
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, (str, Path)):
        return Path(value).read_bytes()
    raise ValueError("correctness diagnostic evidence is not bytes or a path")


def _manifest_sha(value: Any, *keys: str) -> str:
    current = value
    for key in keys:
        if not isinstance(current, Mapping):
            return ""
        current = current.get(key)
    text = str(current or "").lower()
    if re.fullmatch(r"[0-9a-f]{64}", text) is None:
        return ""
    return text


def _normalized_dependency_names(rows: Any) -> set[str] | None:
    if not isinstance(rows, list) or not rows:
        return None
    names: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        if row.get("kind") != "windows_inbox":
            return None
        if row.get("distributed_bytes") != 0:
            return None
        name = str(row.get("name", "")).lower().removesuffix(".dll")
        if not name:
            return None
        names.add(name)
    return names


@functools.lru_cache(maxsize=1)
def _dependency_inventory() -> dict[str, Any]:
    value = json.loads(
        DEPENDENCY_INVENTORY_PATH.read_text(encoding="utf-8")
    )
    if not isinstance(value, dict):
        raise ValueError("dependency inventory is not a JSON object")
    return value


def _normalized_imports(values: Any) -> set[str]:
    return {
        str(item).lower().removesuffix(".dll")
        for item in (values or [])
        if str(item).strip()
    }


def _manifest_self_digest_ok(manifest: Mapping[str, Any]) -> bool:
    declared = str(manifest.get("manifest_sha256", ""))
    if re.fullmatch(r"sha256:v1:[0-9a-f]{64}", declared) is None:
        return False
    without_digest = {
        key: value for key, value in manifest.items() if key != "manifest_sha256"
    }
    computed = "sha256:v1:" + hashlib.sha256(
        nwp.canonical_json_bytes(without_digest)
    ).hexdigest()
    return declared == computed



def _verify_artifact_closure(
    *,
    artifact_paths: Mapping[str, Any] | None,
    validated_mode: str,
    declared_intro: Mapping[str, Any],
) -> dict[str, Any]:
    """Extract certification facts from artifact bytes, never from summary JSON.

    The feasibility report is a consumer of these facts.  It is not allowed to
    promote mutually-consistent report fields into ground truth.
    """
    if artifact_paths is None:
        raise ValueError(
            "artifact-backed feasibility verification requires canonical, compact, "
            "selected, package, and manifest paths"
        )
    required = (
        "selected_executable",
        "canonical_executable",
        "compact_executable",
        "package",
        "manifest",
    )
    paths: dict[str, Path] = {}
    for key in required:
        raw = artifact_paths.get(key)
        if raw is None:
            raise ValueError(f"artifact-backed feasibility verification is missing {key}")
        path = Path(raw)
        if not path.is_file():
            raise ValueError(f"artifact-backed feasibility input is unavailable: {path}")
        paths[key] = path

    package_bytes = paths["package"].read_bytes()
    package_summary = compact.package_summary(package_bytes)
    package_sha = hashlib.sha256(package_bytes).hexdigest()
    package_size = len(package_bytes)
    if package_summary.get("package_sha256") != package_sha:
        raise ValueError("package summary SHA-256 does not match the package bytes")
    if package_summary.get("package_bytes") != package_size:
        raise ValueError("package summary byte count does not match the package bytes")
    if package_summary.get("profile_id") != common_contract.PROFILE_ID:
        raise ValueError("restricted intro package changed the common profile")
    if package_summary.get("requirements_digest") != common_contract.REQUIREMENTS_DIGEST:
        raise ValueError("restricted intro package changed the requirements digest")
    shader_bytes = package_summary.get("shader_bytes")
    if not _is_positive_int(shader_bytes):
        raise ValueError("restricted intro package has no measured shader payload")

    manifest_bytes = paths["manifest"].read_bytes()
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("restricted intro manifest is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise ValueError("restricted intro manifest is not a JSON object")
    if not _manifest_self_digest_ok(manifest):
        raise ValueError("restricted intro manifest self-digest is invalid")
    manifest_shaders = manifest.get("shaders")
    if not isinstance(manifest_shaders, list) or not manifest_shaders:
        raise ValueError("restricted intro manifest has no shader byte ledger")
    manifest_shader_bytes = 0
    for row in manifest_shaders:
        if not isinstance(row, Mapping) or not _is_positive_int(row.get("blob_bytes")):
            raise ValueError("restricted intro manifest has invalid shader byte accounting")
        manifest_shader_bytes += int(row["blob_bytes"])
    if manifest_shader_bytes != int(shader_bytes):
        raise ValueError(
            "manifest shader byte ledger does not match shader bytes decoded from .nwp"
        )

    manifest_binding = hashlib.sha256(manifest_bytes).digest()
    canonical_executable = paths["canonical_executable"].read_bytes()
    canonical_payload, canonical_manifest_binding = nw05_export.extract_embedded_package(
        canonical_executable
    )
    if canonical_payload != package_bytes:
        raise ValueError("canonical executable does not embed the exact canonical package")
    if canonical_manifest_binding != manifest_binding:
        raise ValueError("canonical executable manifest binding does not match the manifest")

    compact_executable = paths["compact_executable"].read_bytes()
    compact_payload, compact_manifest_binding = nw05_export.extract_embedded_package(
        compact_executable
    )
    if compact_manifest_binding != manifest_binding:
        raise ValueError("compact executable manifest binding does not match the manifest")
    if not compact.is_compact_container(compact_payload):
        raise ValueError("compact candidate does not embed a compact container")
    compact_header = compact.parse_header(compact_payload)
    decoded_package = compact.decode_compact(compact_payload)
    if decoded_package != package_bytes:
        raise ValueError("compact candidate does not decode to the exact canonical package")

    selected_executable = paths["selected_executable"].read_bytes()
    expected_selected = (
        compact_executable if validated_mode == "compact" else canonical_executable
    )
    if validated_mode in {"compact", "canonical_fallback"}:
        if selected_executable != expected_selected:
            raise ValueError(
                "selected executable bytes do not match the validated packaging mode"
            )

    canonical_imports = _normalized_imports(
        nw05_export._pe_runtime_dependencies(canonical_executable)
    )
    compact_imports = _normalized_imports(
        nw05_export._pe_runtime_dependencies(compact_executable)
    )
    selected_imports = _normalized_imports(
        nw05_export._pe_runtime_dependencies(selected_executable)
    )
    if not canonical_imports or not compact_imports or not selected_imports:
        raise ValueError("PE import extraction returned an empty dependency closure")

    manifest_dependencies = _normalized_imports(
        (manifest.get("retained") or {}).get("dependencies") or []
    )
    if manifest_dependencies != canonical_imports:
        raise ValueError(
            "manifest dependency closure does not match canonical PE imports"
        )
    if compact_imports != canonical_imports | {"cabinet"}:
        raise ValueError(
            "compact PE imports differ from canonical imports by more than cabinet.dll"
        )
    expected_selected_imports = (
        compact_imports if validated_mode == "compact" else canonical_imports
    )
    if validated_mode in {"compact", "canonical_fallback"} and (
        selected_imports != expected_selected_imports
    ):
        raise ValueError("selected PE imports do not match the validated artifact")

    inventory = _dependency_inventory()
    closures = inventory.get("closures")
    closures = closures if isinstance(closures, Mapping) else {}
    compact_spec = closures.get("compact_experiment")
    compact_spec = compact_spec if isinstance(compact_spec, Mapping) else {}
    allowed_imports = _normalized_imports(compact_spec.get("allowed_import_dlls"))
    inbox_imports = _normalized_imports(
        [
            item.get("dll_name")
            for item in inventory.get("inbox_windows_allowlist", [])
            if isinstance(item, Mapping)
        ]
    )
    forbidden_imports = _normalized_imports(
        inventory.get("forbidden_release_imports")
    )
    for label, imports in (
        ("canonical", canonical_imports),
        ("compact", compact_imports),
        ("selected", selected_imports),
    ):
        if not imports <= allowed_imports or not imports <= inbox_imports:
            raise ValueError(f"{label} PE imports escape the authoritative inbox allowlist")
        if imports & forbidden_imports:
            raise ValueError(f"{label} PE imports contain a forbidden release dependency")

    canonical_size = len(canonical_executable)
    compact_size = len(compact_executable)
    compact_payload_sha = hashlib.sha256(compact_payload).hexdigest()
    compact_payload_bytes = len(compact_payload)
    footer_bytes = nw05_export.EMBED_FOOTER_SIZE
    manifest_binding_bytes = len(manifest_binding)
    canonical_player_image = (
        canonical_size - package_size - manifest_binding_bytes - footer_bytes
    )
    compact_player_image = (
        compact_size - compact_payload_bytes - manifest_binding_bytes - footer_bytes
    )
    if canonical_player_image <= 0 or compact_player_image <= 0:
        raise ValueError("artifact closure has a non-positive player image component")

    facts = {
        "package_sha256": package_sha,
        "package_bytes": package_size,
        "shader_bytes": int(shader_bytes),
        "profile_id": package_summary["profile_id"],
        "requirements_digest": package_summary["requirements_digest"],
        "manifest_sha256": manifest_sha,
        "manifest_bytes": len(manifest_bytes),
        "manifest": manifest,
        "canonical_executable_sha256": hashlib.sha256(canonical_executable).hexdigest(),
        "canonical_executable_bytes": canonical_size,
        "canonical_player_image_bytes": canonical_player_image,
        "canonical_imports": sorted(canonical_imports),
        "compact_executable_sha256": hashlib.sha256(compact_executable).hexdigest(),
        "compact_executable_bytes": compact_size,
        "compact_player_image_bytes": compact_player_image,
        "compact_payload_sha256": compact_payload_sha,
        "compact_payload_bytes": compact_payload_bytes,
        "compact_codec_id": str(compact_header.get("codec_id", "")),
        "compact_layout_id": str(compact_header.get("layout_id", "")),
        "compact_imports": sorted(compact_imports),
        "selected_executable_sha256": hashlib.sha256(selected_executable).hexdigest(),
        "selected_executable_bytes": len(selected_executable),
        "selected_imports": sorted(selected_imports),
        "manifest_binding_bytes": manifest_binding_bytes,
        "embedded_footer_bytes": footer_bytes,
        "inventory_validated": True,
    }

    canonical_intro, compact_intro = _intro_artifacts(declared_intro)
    declared_checks = (
        ("canonical package SHA", str(canonical_intro.get("package_sha256", "")).lower(), package_sha),
        ("canonical package bytes", canonical_intro.get("package_bytes"), package_size),
        ("canonical manifest SHA", _manifest_sha(canonical_intro, "manifest_sha256"), manifest_sha),
        ("canonical executable SHA", str(canonical_intro.get("executable_sha256", "")).lower(), facts["canonical_executable_sha256"]),
        ("canonical executable bytes", canonical_intro.get("executable_bytes"), canonical_size),
        ("compact executable SHA", str(compact_intro.get("executable_sha256", "")).lower(), facts["compact_executable_sha256"]),
        ("compact executable bytes", compact_intro.get("executable_bytes"), compact_size),
        ("compact payload SHA", str(compact_intro.get("encoded_package_sha256", "")).lower(), compact_payload_sha),
        ("compact payload bytes", compact_intro.get("encoded_package_bytes"), compact_payload_bytes),
        ("compact codec", str(compact_intro.get("codec_id", "")), facts["compact_codec_id"]),
        ("compact layout", str(compact_intro.get("layout_id", "")), facts["compact_layout_id"]),
    )
    for label, declared, actual in declared_checks:
        if declared != actual:
            raise ValueError(f"intro report {label} does not match artifact-backed facts")
    if canonical_intro.get("link_manifest") != manifest:
        raise ValueError("intro report link_manifest does not match the manifest file")
    measurements = declared_intro.get("measurements")
    measurements = measurements if isinstance(measurements, Mapping) else {}
    package_measurement = measurements.get("package")
    package_measurement = (
        package_measurement if isinstance(package_measurement, Mapping) else {}
    )
    if (
        str(package_measurement.get("package_sha256", "")).lower() != package_sha
        or package_measurement.get("package_bytes") != package_size
        or package_measurement.get("shader_bytes") != shader_bytes
    ):
        raise ValueError(
            "intro package measurements do not match package bytes decoded from .nwp"
        )
    return facts


def _verification(
    status: str, reason: str, **details: Any
) -> dict[str, Any]:
    return {"status": status, "reason": reason, **details}


def _validate_evidence_schema(
    value: Mapping[str, Any], schema_name: str
) -> tuple[bool, str]:
    path = REPO_ROOT / "schemas" / schema_name
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        document = dict(value)
        validate_instance(document, schema, schema)
        validate_semantic_invariants(document, schema_name, path)
    except (OSError, ValueError, SchemaValidationError) as exc:
        return False, str(exc)
    return True, ""


def _certify_compact_candidate(
    *,
    intro: Mapping[str, Any],
    validated_mode: str,
    validated_sha: str,
    validated_size: int,
    head_sha: str,
    pe_ledger: Mapping[str, Any] | None,
    dependency_closure: Mapping[str, Any] | None,
    system_validation: Mapping[str, Any] | None,
    performance: Mapping[str, Any] | None,
    correctness: Mapping[str, Any] | None,
    correctness_evidence: Mapping[str, Any] | None,
    perf_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify every acceptance gate required to publish a 64 KiB success."""
    canonical_intro, compact_intro = _intro_artifacts(intro)
    ledgers = intro.get("ledgers")
    ledgers = ledgers if isinstance(ledgers, Mapping) else {}
    canonical_ledger = ledgers.get("canonical")
    canonical_ledger = (
        canonical_ledger if isinstance(canonical_ledger, Mapping) else {}
    )
    compact_ledger = ledgers.get("compact")
    compact_ledger = (
        compact_ledger if isinstance(compact_ledger, Mapping) else {}
    )
    ledger = compact_ledger if validated_mode == "compact" else canonical_ledger
    components = ledger.get("components")
    components = components if isinstance(components, Mapping) else {}
    canonical_package_sha = str(
        canonical_intro.get("package_sha256", "")
    ).lower()
    canonical_package_bytes = canonical_intro.get("package_bytes")
    canonical_manifest_sha = _manifest_sha(canonical_intro, "manifest_sha256")
    canonical_artifact_key = str(
        canonical_intro.get("artifact_key", "")
    ).strip()
    compact_encoded_sha = str(
        compact_intro.get("encoded_package_sha256", "")
    ).lower()
    compact_encoded_bytes = compact_intro.get("encoded_package_bytes")
    compact_codec_id = str(compact_intro.get("codec_id", ""))
    compact_layout_id = str(compact_intro.get("layout_id", ""))
    compact_components = compact_ledger.get("components")
    compact_components = (
        compact_components if isinstance(compact_components, Mapping) else {}
    )
    canonical_components = canonical_ledger.get("components")
    canonical_components = (
        canonical_components if isinstance(canonical_components, Mapping) else {}
    )
    intro_measurements = intro.get("measurements")
    intro_measurements = (
        intro_measurements if isinstance(intro_measurements, Mapping) else {}
    )
    intro_package_measurements = intro_measurements.get("package")
    intro_package_measurements = (
        intro_package_measurements
        if isinstance(intro_package_measurements, Mapping)
        else {}
    )
    link_manifest = canonical_intro.get("link_manifest")
    link_manifest = (
        link_manifest if isinstance(link_manifest, Mapping) else {}
    )
    link_shaders = link_manifest.get("shaders")
    link_shaders = link_shaders if isinstance(link_shaders, list) else []
    link_shader_blob_bytes: list[int] = []
    link_shader_bytes_ok = bool(link_shaders)
    for shader in link_shaders:
        if not isinstance(shader, Mapping) or not _is_positive_int(
            shader.get("blob_bytes")
        ):
            link_shader_bytes_ok = False
            break
        link_shader_blob_bytes.append(int(shader["blob_bytes"]))
    link_shader_bytes = sum(link_shader_blob_bytes)
    link_manifest_present = bool(link_manifest) and bool(link_shaders)
    link_manifest_file_sha = hashlib.sha256(
        json.dumps(link_manifest, indent=2, sort_keys=True).encode("utf-8")
    ).hexdigest()
    manifest_file_sha_ok = link_manifest_file_sha == canonical_manifest_sha
    link_manifest_ok = (
        link_manifest_present
        and link_shader_bytes_ok
        and _manifest_self_digest_ok(link_manifest)
        and manifest_file_sha_ok
    )
    intro_package_measurement_present = all(
        key in intro_package_measurements
        for key in ("package_sha256", "package_bytes", "shader_bytes")
    )
    measured_shader_bytes = intro_package_measurements.get("shader_bytes")
    intro_package_measurement_ok = (
        intro_package_measurement_present
        and str(
            intro_package_measurements.get("package_sha256", "")
        ).lower()
        == canonical_package_sha
        and intro_package_measurements.get("package_bytes")
        == canonical_package_bytes
    )
    intro_shader_measurement_present = (
        intro_package_measurement_present and link_manifest_present
    )
    intro_shader_measurement_ok = (
        intro_package_measurement_ok
        and link_manifest_ok
        and _is_positive_int(measured_shader_bytes)
        and int(measured_shader_bytes) == link_shader_bytes
    )
    dependency_closure_available = isinstance(dependency_closure, Mapping)
    dependency_closure_valid = False
    expected_selected_dependencies: set[str] | None = None
    expected_compact_dependencies: set[str] | None = None
    if dependency_closure_available:
        inventory = _dependency_inventory()
        closures = inventory.get("closures")
        closures = closures if isinstance(closures, Mapping) else {}
        profile = str(dependency_closure.get("profile", ""))
        closure_spec = closures.get(profile)
        closure_spec = (
            closure_spec if isinstance(closure_spec, Mapping) else {}
        )
        allowed_imports = _normalized_imports(
            closure_spec.get("allowed_import_dlls")
        )
        inbox_imports = _normalized_imports(
            [
                item.get("dll_name")
                for item in inventory.get("inbox_windows_allowlist", [])
                if isinstance(item, Mapping)
            ]
        )
        forbidden_imports = _normalized_imports(
            inventory.get("forbidden_release_imports")
        )
        imported = _normalized_imports(
            dependency_closure.get("canonical_imports")
        )
        sidecars = list(dependency_closure.get("sidecar_files") or [])
        reported_binary_sha = str(
            dependency_closure.get("binary_sha256", "")
        ).lower()
        dependency_closure_valid = (
            profile == "compact_experiment"
            and dependency_closure.get("closure_verified") is True
            and bool(imported)
            and imported <= allowed_imports
            and imported <= inbox_imports
            and not (imported & forbidden_imports)
            and not sidecars
            and reported_binary_sha == validated_sha
        )
        if dependency_closure_valid:
            expected_selected_dependencies = imported
            expected_compact_dependencies = imported | {"cabinet"}
    compact_dependency_rows_present = bool(
        isinstance(compact_ledger.get("dependencies"), list)
        and compact_ledger.get("dependencies")
    )
    compact_dependency_names = _normalized_dependency_names(
        compact_ledger.get("dependencies")
    )
    compact_dependencies_ok = (
        compact_dependency_names is not None
        and "cabinet" in compact_dependency_names
        and (
            expected_compact_dependencies is None
            or compact_dependency_names == expected_compact_dependencies
        )
    )
    compact_ledger_identity_applicable = True
    compact_compression_rejected = compact_ledger.get(
        "compression_rejected_for_increasing_distribution"
    )
    if (
        validated_mode == "canonical_fallback"
        and compact_compression_rejected is True
    ):
        expected_compact_encoded_bytes = canonical_package_bytes
        compact_encoded_payload_mode = (
            "compression_rejected_canonical_package_bytes"
        )
    else:
        expected_compact_encoded_bytes = compact_encoded_bytes
        compact_encoded_payload_mode = "measured_compact_payload"
    compact_all_accounted = compact_ledger.get(
        "all_required_bytes_accounted"
    )
    compact_unaccounted = list(
        compact_ledger.get("unaccounted_components") or []
    )
    compact_decoder_bytes = compact_components.get("decoder_code_bytes")
    compact_decoder_accounting_ok = (
        isinstance(compact_all_accounted, bool)
        and (
            (
                compact_all_accounted
                and not compact_unaccounted
                and _is_positive_int(compact_decoder_bytes)
            )
            or (
                not compact_all_accounted
                and set(compact_unaccounted) == {"decoder_code_bytes"}
                and compact_decoder_bytes is None
            )
        )
    )
    compact_complete_bytes = compact_ledger.get(
        "complete_runnable_distribution_bytes"
    )
    compact_selected_bytes = compact_ledger.get(
        "selected_executable_bytes"
    )
    compact_component_total: int | None
    try:
        compact_component_total = sum(
            int(compact_components.get(key, -1))
            for key in (
                "player_image_bytes",
                "encoded_package_bytes",
                "embedded_manifest_binding_bytes",
                "embedded_footer_bytes",
            )
        )
    except (TypeError, ValueError):
        compact_component_total = None
    compact_ledger_identity_ok = (
        bool(compact_ledger)
        and re.fullmatch(r"[0-9a-f]{64}", compact_encoded_sha) is not None
        and _is_positive_int(compact_encoded_bytes)
        and _is_positive_int(canonical_package_bytes)
        and compact_components.get("canonical_package_bytes")
        == canonical_package_bytes
        and str(compact_ledger.get("codec_id", "")) == compact_codec_id
        and str(compact_ledger.get("layout_id", "")) == compact_layout_id
        and isinstance(compact_compression_rejected, bool)
        and compact_components.get("encoded_package_bytes")
        == expected_compact_encoded_bytes
        and compact_complete_bytes == compact_selected_bytes
        and compact_complete_bytes == compact_component_total
        and all(
            _is_positive_int(compact_components.get(key))
            for key in (
                "player_image_bytes",
                "encoded_package_bytes",
                "embedded_manifest_binding_bytes",
                "embedded_footer_bytes",
            )
        )
        and compact_components.get("embedded_manifest_binding_bytes") == 32
        and compact_components.get("embedded_footer_bytes") == 92
        and intro_shader_measurement_ok
        and compact_components.get("shader_payload_bytes")
        == measured_shader_bytes
        and compact_components.get("shader_payload_bytes")
        <= compact_components.get("player_image_bytes")
        and (
            not canonical_components
            or compact_components.get("shader_payload_bytes")
            == canonical_components.get("shader_payload_bytes")
        )
        and not compact_ledger.get("required_auxiliary_files")
        and compact_dependency_rows_present
        and compact_dependencies_ok
        and (not dependency_closure_available or dependency_closure_valid)
        and compact_decoder_accounting_ok
        and (
            validated_mode != "compact"
            or (
                compact_compression_rejected is False
                and compact_complete_bytes == validated_size
            )
        )
        and (
            validated_mode != "canonical_fallback"
            or compact_compression_rejected is not True
            or compact_complete_bytes == validated_size
        )
    )
    pe_manifest_sha = (
        _manifest_sha(pe_ledger, "final_artifact", "manifest_sha256")
        if pe_ledger is not None
        else ""
    )
    decoded_package_sha = str(
        compact_intro.get("decoded_package_sha256", "")
    ).lower()
    decoded_bytes_present = (
        compact_intro.get("decoded_package_bytes") is not None
        and canonical_package_bytes is not None
    )
    decoded_match = (
        bool(decoded_package_sha)
        and decoded_package_sha == canonical_package_sha
        and (
            not decoded_bytes_present
            or int(compact_intro.get("decoded_package_bytes", -1))
            == int(canonical_package_bytes)
        )
    )
    decoded_identity = _verification(
        "verified" if decoded_match else "failed",
        (
            "compact payload decodes to the exact canonical package"
            if decoded_match
            else "compact payload does not decode to the canonical package"
        ),
        canonical_package_sha256=canonical_package_sha or None,
        decoded_package_sha256=decoded_package_sha or None,
        canonical_package_bytes=canonical_package_bytes,
        decoded_package_bytes=compact_intro.get("decoded_package_bytes"),
        validated_mode=validated_mode,
    )

    measured_decoder_bytes = components.get("decoder_code_bytes")
    if (
        validated_mode == "compact"
        and measured_decoder_bytes is None
        and pe_ledger is not None
    ):
        decoder = pe_ledger.get("compact_decoder")
        if isinstance(decoder, Mapping):
            measured_decoder_bytes = decoder.get("template_bytes_added")
    unaccounted = list(ledger.get("unaccounted_components") or [])
    unaccounted_set = set(unaccounted)
    all_required = ledger.get("all_required_bytes_accounted")
    all_required_true = all_required is True and not unaccounted
    ledger_accounting_consistent = all_required_true or (
        validated_mode == "compact"
        and all_required is False
        and unaccounted_set == {"decoder_code_bytes"}
    )
    decoder_pending_ok = (
        validated_mode == "compact"
        and all_required is False
        and unaccounted_set == {"decoder_code_bytes"}
        and measured_decoder_bytes is not None
    )
    bytes_accounted = all_required_true or decoder_pending_ok
    required_auxiliary = ledger.get("required_auxiliary_files")
    no_required_sidecars = not required_auxiliary
    selected_dependency_rows_present = bool(
        isinstance(ledger.get("dependencies"), list)
        and ledger.get("dependencies")
    )
    selected_dependency_names = _normalized_dependency_names(
        ledger.get("dependencies")
    )
    selected_dependencies_ok = (
        selected_dependency_names is not None
        and (
            expected_selected_dependencies is None
            or selected_dependency_names == expected_selected_dependencies
        )
    )
    expected_ledger_codec = (
        compact_codec_id
        if validated_mode == "compact"
        else contract.CODEC_STORED_ID
    )
    expected_ledger_layout = (
        compact_layout_id
        if validated_mode == "compact"
        else contract.LAYOUT_CANONICAL_ID
    )
    expected_ledger_payload_bytes = (
        compact_encoded_bytes
        if validated_mode == "compact"
        else canonical_package_bytes
    )
    selected_component_total: int | None
    try:
        selected_component_total = sum(
            int(components.get(key, -1))
            for key in (
                "player_image_bytes",
                "encoded_package_bytes",
                "embedded_manifest_binding_bytes",
                "embedded_footer_bytes",
            )
        )
    except (TypeError, ValueError):
        selected_component_total = None
    ledger_identity_present = (
        _is_positive_int(ledger.get("complete_runnable_distribution_bytes"))
        and _is_positive_int(ledger.get("selected_executable_bytes"))
        and _is_positive_int(components.get("canonical_package_bytes"))
        and _is_positive_int(components.get("encoded_package_bytes"))
        and _is_positive_int(components.get("player_image_bytes"))
        and _is_positive_int(
            components.get("embedded_manifest_binding_bytes")
        )
        and _is_positive_int(components.get("embedded_footer_bytes"))
        and _is_positive_int(components.get("shader_payload_bytes"))
        and intro_shader_measurement_present
        and selected_dependency_rows_present
        and bool(str(ledger.get("codec_id", "")))
        and bool(str(ledger.get("layout_id", "")))
    )
    ledger_identity_ok = (
        ledger_identity_present
        and intro_shader_measurement_ok
        and ledger.get("complete_runnable_distribution_bytes") == validated_size
        and ledger.get("selected_executable_bytes") == validated_size
        and ledger.get("complete_runnable_distribution_bytes")
        == selected_component_total
        and components.get("embedded_manifest_binding_bytes") == 32
        and components.get("embedded_footer_bytes") == 92
        and components.get("shader_payload_bytes") == measured_shader_bytes
        and components.get("shader_payload_bytes")
        <= components.get("player_image_bytes")
        and components.get("canonical_package_bytes") == canonical_package_bytes
        and components.get("encoded_package_bytes")
        == expected_ledger_payload_bytes
        and str(ledger.get("codec_id", "")) == expected_ledger_codec
        and str(ledger.get("layout_id", "")) == expected_ledger_layout
        and selected_dependencies_ok
        and (not dependency_closure_available or dependency_closure_valid)
        and ledger_accounting_consistent
    )
    bytes_ok = bytes_accounted and no_required_sidecars and ledger_identity_ok
    bytes_status = (
        "verified"
        if bytes_ok
        else (
            "failed"
            if ledger_identity_present and not ledger_identity_ok
            else "pending"
        )
    )
    bytes_verification = _verification(
        bytes_status,
        (
            "all complete-distribution bytes and selected-artifact identities are accounted"
            if bytes_ok
            else (
                "byte ledger describes a different selected artifact or payload"
                if bytes_status == "failed"
                else "complete-distribution byte accounting or identity binding is incomplete"
            )
        ),
        all_required_bytes_accounted=bytes_accounted,
        selected_artifact_identity_verified=ledger_identity_ok,
        complete_runnable_distribution_bytes=ledger.get(
            "complete_runnable_distribution_bytes"
        ),
        selected_executable_bytes=ledger.get("selected_executable_bytes"),
        canonical_package_bytes=components.get("canonical_package_bytes"),
        encoded_package_bytes=components.get("encoded_package_bytes"),
        codec_id=ledger.get("codec_id"),
        layout_id=ledger.get("layout_id"),
        decoder_code_bytes=measured_decoder_bytes,
        unaccounted_components=unaccounted,
        required_auxiliary_files=dict(required_auxiliary or {}),
    )

    dependency_rows = ledger.get("dependencies")
    dependency_rows = (
        dependency_rows if isinstance(dependency_rows, list) else []
    )
    dependency_names = {
        str(item.get("name", "")).lower().removesuffix(".dll")
        for item in dependency_rows
        if isinstance(item, Mapping)
    }
    if dependency_closure is not None:
        sidecars = dependency_closure.get("sidecar_files")
        reported_binary_sha = str(
            dependency_closure.get("binary_sha256", "")
        ).lower()
        imported_dlls = {
            str(item).lower().removesuffix(".dll")
            for item in (dependency_closure.get("canonical_imports") or [])
        }
        closure_verified = (
            dependency_closure_valid
            and dependency_closure.get("fixture_id") == "nw10_validated"
            and dependency_closure.get("binary_bytes") == validated_size
            and isinstance(sidecars, list)
            and not sidecars
            and bool(imported_dlls)
            and imported_dlls <= dependency_names
            and (
                validated_mode != "compact"
                or "cabinet" in imported_dlls
            )
        )
        dependency_verification = _verification(
            "verified" if closure_verified else "failed",
            (
                "dependency closure is verified against the exact candidate executable"
                if closure_verified
                else "dependency closure is not bound to the candidate executable"
            ),
            sidecar_files=list(sidecars or []),
            imported_dlls=list(dependency_closure.get("canonical_imports") or []),
            fixture_id=dependency_closure.get("fixture_id"),
            profile=dependency_closure.get("profile"),
            binary_sha256=reported_binary_sha or None,
            binary_bytes=dependency_closure.get("binary_bytes"),
            binary_binding="verified" if closure_verified else "failed",
            inventory_validated=dependency_closure_valid,
        )
    else:
        dependency_verification = _verification(
            "pending",
            "no measured dependency-closure report was supplied",
            sidecar_files=[],
            imported_dlls=sorted(dependency_names),
        )

    pe_verification: dict[str, Any]
    if pe_ledger is None:
        pe_verification = _verification(
            "pending", "no measured PE ledger was supplied"
        )
    else:
        schema_ok, schema_error = _validate_evidence_schema(
            pe_ledger, "nw10-windows-pe-ledger.schema.json"
        )
        final = pe_ledger.get("final_artifact")
        final = final if isinstance(final, Mapping) else {}
        linker = pe_ledger.get("linker_elimination")
        linker = linker if isinstance(linker, Mapping) else {}
        decoder = pe_ledger.get("compact_decoder")
        decoder = decoder if isinstance(decoder, Mapping) else {}
        conclusion = pe_ledger.get("conclusion")
        conclusion = conclusion if isinstance(conclusion, Mapping) else {}
        mitigation_bits = linker.get("security_mitigation_bits")
        mitigation_bits = (
            mitigation_bits if isinstance(mitigation_bits, Mapping) else {}
        )
        expected_payload_kind = (
            "compact_container"
            if validated_mode == "compact"
            else "canonical_package"
        )
        expected_payload_sha = (
            compact_encoded_sha
            if validated_mode == "compact"
            else canonical_package_sha
        )
        expected_payload_bytes = (
            compact_encoded_bytes
            if validated_mode == "compact"
            else canonical_package_bytes
        )
        pe_ok = (
            schema_ok
            and pe_ledger.get("format") == "nw10-windows-pe-ledger"
            and pe_ledger.get("status") == "verified"
            and str(
                (pe_ledger.get("source") or {}).get("head_sha", "")
            ).lower()
            == head_sha
            and str(final.get("executable_sha256", "")).lower()
            == validated_sha
            and int(final.get("executable_bytes", -1)) == validated_size
            and str(final.get("package_sha256", "")).lower()
            == canonical_package_sha
            and bool(canonical_manifest_sha)
            and pe_manifest_sha == canonical_manifest_sha
            and final.get("package_binding_verified") is True
            and final.get("manifest_binding_verified") is True
            and str(final.get("embedded_payload_sha256", "")).lower()
            == expected_payload_sha
            and final.get("embedded_payload_bytes") == expected_payload_bytes
            and str(final.get("decoded_package_sha256", "")).lower()
            == canonical_package_sha
            and final.get("runtime_status") == "validated_runnable"
            and final.get("runtime_exit_code") == 0
            and final.get("embedded_payload_kind") == expected_payload_kind
            and linker.get("security_mitigations_preserved") is True
            and mitigation_bits.get("control_required_bits_present") is True
            and mitigation_bits.get("decoder_on_required_bits_present") is True
            and linker.get("canonical_package_identical") is True
            and linker.get("embedded_payload_sha256_identical") is True
            and linker.get("embedded_payload_size_identical") is True
            and linker.get("final_payload_kind") == expected_payload_kind
            and linker.get("control_payload_kind") == expected_payload_kind
            and linker.get("ltgc_preserved") is True
            and (
                (
                    validated_mode == "compact"
                    and linker.get("compact_decoder_preserved") is True
                    and decoder.get("compact_payload_verified") is True
                )
                or (
                    validated_mode == "canonical_fallback"
                    and linker.get("compact_decoder_preserved") is False
                    and decoder.get("compact_payload_verified") is False
                )
            )
            and decoder.get("final_payload_kind") == expected_payload_kind
            and str(decoder.get("final_payload_sha256", "")).lower()
            == expected_payload_sha
            and str(decoder.get("final_decoded_package_sha256", "")).lower()
            == canonical_package_sha
            and conclusion.get("byte_ledger_complete") is True
        )
        pe_verification = _verification(
            "verified"
            if pe_ok
            else (
                "failed"
                if pe_ledger.get("status") == "verified"
                else "pending"
            ),
            (
                "PE ledger binds the validated artifact, package, mitigations, "
                "LTGC, and payload mode"
                if pe_ok
                else "PE ledger does not bind every required candidate property"
            ),
            schema_error=schema_error or None,
            executable_sha256=final.get("executable_sha256"),
            executable_bytes=final.get("executable_bytes"),
            security_mitigations_preserved=linker.get(
                "security_mitigations_preserved"
            ),
            ltgc_preserved=linker.get("ltgc_preserved"),
            compact_decoder_preserved=linker.get("compact_decoder_preserved"),
        )

    system = system_validation or {}
    clean = system.get("clean_system")
    clean = clean if isinstance(clean, Mapping) else {}
    deployment = clean.get("deployment")
    deployment = deployment if isinstance(deployment, Mapping) else {}
    host = clean.get("host")
    host = host if isinstance(host, Mapping) else {}
    scenarios = clean.get("scenarios")
    scenarios = scenarios if isinstance(scenarios, list) else []
    expected_scenarios = clean_system_contract.EXPECTED_SCENARIO_EXIT_CODES
    scenario_names = {
        str(row.get("scenario", ""))
        for row in scenarios
        if isinstance(row, Mapping)
    }
    scenarios_ok = scenario_names == set(expected_scenarios) and all(
        isinstance(row, Mapping)
        and row.get("status") == "passed"
        and row.get("timed_out") is False
        and row.get("expected_exit_code")
        == expected_scenarios[str(row.get("scenario", ""))]
        and row.get("observed_exit_code")
        == expected_scenarios[str(row.get("scenario", ""))]
        for row in scenarios
    )
    system_final = system.get("final_artifact")
    system_final = (
        system_final if isinstance(system_final, Mapping) else {}
    )
    system_schema_ok, system_schema_error = (
        _validate_evidence_schema(
            system, "nw10-system-validation.schema.json"
        )
        if system
        else (False, "not provided")
    )
    clean_hardware = clean.get("hardware")
    clean_hardware = (
        clean_hardware if isinstance(clean_hardware, Mapping) else {}
    )
    clean_windows_build = str(host.get("windows_build", ""))
    privacy = system.get("privacy")
    privacy = privacy if isinstance(privacy, Mapping) else {}
    clean_ok = (
        system_schema_ok
        and system.get("status") == "verified"
        and str(system.get("source_head_sha", "")) == head_sha
        and clean.get("status") == "verified"
        and clean.get("execution_kind") == "physical"
        and deployment.get("only_runtime_executable") is True
        and deployment.get("sidecars") == []
        and deployment.get("forbidden_runtime_dependencies") == []
        and str(deployment.get("executable_sha256", "")).lower()
        == validated_sha
        and int(deployment.get("executable_bytes", -1)) == validated_size
        and host.get("clean_environment") is True
        and isinstance(host.get("virtualization_probe"), Mapping)
        and str(host.get("architecture", "")).lower()
        in {"amd64", "x86_64"}
        and clean.get("development_dependencies_present_on_path") in ([], ())
        and clean.get("environment_cache_variables") in ([], ())
        and scenarios_ok
        and str(system_final.get("executable_sha256", "")).lower()
        == validated_sha
        and int(system_final.get("executable_bytes", -1)) == validated_size
        and str(system_final.get("package_sha256", "")).lower()
        == canonical_package_sha
        and bool(canonical_manifest_sha)
        and _manifest_sha(
            system_validation, "final_artifact", "manifest_sha256"
        )
        == canonical_manifest_sha
        and bool(clean_windows_build)
        and str(system.get("windows_build", "")) == clean_windows_build
        and privacy.get("local_paths_included") is False
        and privacy.get("raw_stdout_stderr_included") is False
        and privacy.get("user_profile_paths_included") is False
        and privacy.get("raw_result_bundle_tracked") is False
    )
    clean_verification = _verification(
        "verified"
        if clean_ok
        else (
            "failed"
            if system.get("status") == "verified"
            else "pending"
        ),
        (
            "clean target deployment and frozen scenarios are verified"
            if clean_ok
            else "clean-system physical validation is unavailable or incomplete"
        ),
        schema_error=system_schema_error or None,
        deployment_executable_sha256=deployment.get("executable_sha256"),
        scenario_count=len(scenarios),
    )

    startup = system.get("startup")
    startup = startup if isinstance(startup, Mapping) else {}
    startup_value = startup.get("process_start_to_first_present_ms")
    startup_hardware = startup.get("hardware")
    startup_hardware = (
        startup_hardware if isinstance(startup_hardware, Mapping) else {}
    )
    startup_ok = (
        system_schema_ok
        and startup.get("status") == "verified"
        and startup.get("execution_kind") == "physical"
        and startup_value is not None
        and 0.0 <= float(startup_value) <= contract.STARTUP_CEILING_MS
        and startup.get("ceiling_ms") == contract.STARTUP_CEILING_MS
        and isinstance(startup.get("cache_state"), Mapping)
        and startup["cache_state"].get("application_pipeline_cache_used")
        is False
        and startup["cache_state"].get("kind")
        in {"cold_driver_cache", "warm"}
        and str(startup_hardware.get("adapter_description", ""))
        == str(clean_hardware.get("adapter_description", ""))
        and str(startup_hardware.get("driver_version", ""))
        == str(clean_hardware.get("driver_version", ""))
        and str(startup_hardware.get("windows_build", ""))
        == clean_windows_build
    )
    startup_verification = _verification(
        "verified"
        if startup_ok
        else (
            "failed"
            if system.get("status") == "verified"
            else "pending"
        ),
        (
            "startup is verified below the frozen ceiling"
            if startup_ok
            else "physical startup evidence is unavailable or exceeds the ceiling"
        ),
        process_start_to_first_present_ms=startup_value,
        ceiling_ms=contract.STARTUP_CEILING_MS,
    )

    memory = system.get("memory")
    memory = memory if isinstance(memory, Mapping) else {}
    peak = memory.get("peak_engine_committed_gpu_bytes")
    memory_hardware = memory.get("hardware")
    memory_hardware = (
        memory_hardware if isinstance(memory_hardware, Mapping) else {}
    )
    memory_ok = (
        system_schema_ok
        and memory.get("status") == "verified"
        and memory.get("execution_kind") == "physical"
        and peak is not None
        and 0 <= int(peak) <= contract.PEAK_ENGINE_COMMITTED_GPU_BYTES
        and memory.get("ceiling_bytes")
        == contract.PEAK_ENGINE_COMMITTED_GPU_BYTES
        and str(memory.get("package_sha256", "")).lower()
        == canonical_package_sha
        and int(memory.get("post_initialization_allocation_violation", -1)) == 0
        and str(memory_hardware.get("adapter_description", ""))
        == str(clean_hardware.get("adapter_description", ""))
        and str(memory_hardware.get("driver_version", ""))
        == str(clean_hardware.get("driver_version", ""))
        and str(memory_hardware.get("windows_build", ""))
        == clean_windows_build
    )
    memory_verification = _verification(
        "verified"
        if memory_ok
        else (
            "failed"
            if system.get("status") == "verified"
            else "pending"
        ),
        (
            "engine-committed memory is verified below the frozen ceiling"
            if memory_ok
            else "physical memory evidence is unavailable or exceeds the ceiling"
        ),
        peak_engine_committed_gpu_bytes=peak,
        ceiling_bytes=contract.PEAK_ENGINE_COMMITTED_GPU_BYTES,
    )

    performance_ok = False
    performance_details: dict[str, Any] = {}
    performance_reference_bound = False
    expected_reference: dict[str, Any] = {}
    expected_binding_id = ""
    expected_binding_digest = ""
    expected_reference_artifact: dict[str, Any] = {}
    reference_validation: dict[str, Any] = {}
    try:
        reference_validation = nw07_evidence.validate_perf_contract(perf_contract)
    except (KeyError, TypeError, ValueError) as exc:
        reference_validation = {
            "status": "invalid",
            "reasons": [str(exc)],
            "missing_bindings": ["reference_contract"],
        }
    performance_reference_bound = (
        reference_validation.get("status") == "frozen"
    )
    if performance_reference_bound:
        expected_reference = nw10_perf_preservation._technical_reference(
            perf_contract.get("reference") or {}
        )
        expected_binding_id = str(perf_contract.get("binding_id") or "")
        expected_binding_digest = str(
            perf_contract.get("binding_digest") or ""
        )
        reference_artifact = (
            (perf_contract.get("reference") or {}).get("artifact") or {}
        )
        expected_reference_artifact = {
            key: reference_artifact.get(key)
            for key in (
                "artifact_key",
                "package_sha256",
                "manifest_sha256",
                "executable_sha256",
                "complete_distribution_bytes",
            )
        }
    if performance is not None:
        performance_schema_ok, performance_schema_error = (
            _validate_evidence_schema(
                performance, "nw10-performance-preservation.schema.json"
            )
        )
        after = performance.get("after")
        after = after if isinstance(after, Mapping) else {}
        before = performance.get("before")
        before = before if isinstance(before, Mapping) else {}
        runs = after.get("runs")
        runs = runs if isinstance(runs, list) else []
        artifact = after.get("artifact")
        artifact = artifact if isinstance(artifact, Mapping) else {}
        before_artifact = before.get("artifact")
        before_artifact = (
            before_artifact if isinstance(before_artifact, Mapping) else {}
        )
        candidate = performance.get("candidate")
        candidate = candidate if isinstance(candidate, Mapping) else {}
        candidate_artifact = candidate.get("artifact")
        candidate_artifact = (
            candidate_artifact
            if isinstance(candidate_artifact, Mapping)
            else {}
        )
        candidate_graph = candidate.get("common_graph")
        candidate_graph = (
            candidate_graph if isinstance(candidate_graph, Mapping) else {}
        )
        candidate_build_inputs = candidate.get("build_inputs")
        candidate_build_inputs = (
            candidate_build_inputs
            if isinstance(candidate_build_inputs, Mapping)
            else {}
        )
        candidate_dependency = candidate.get("dependency_closure")
        candidate_dependency = (
            candidate_dependency
            if isinstance(candidate_dependency, Mapping)
            else {}
        )
        candidate_runtime = candidate.get("runtime_probe")
        candidate_runtime = (
            candidate_runtime
            if isinstance(candidate_runtime, Mapping)
            else {}
        )

        artifacts_ok = all(
            re.fullmatch(r"[0-9a-f]{64}", str(entry.get(key, "")).lower())
            is not None
            and int(entry.get("complete_distribution_bytes", 0)) > 0
            for entry in (before_artifact, artifact)
            for key in ("package_sha256", "manifest_sha256", "executable_sha256")
        )
        artifacts_ok = (
            artifacts_ok
            and bool(str(before_artifact.get("artifact_key", "")).strip())
            and bool(str(artifact.get("artifact_key", "")).strip())
            and str(before_artifact.get("package_sha256", "")).lower()
            == str(artifact.get("package_sha256", "")).lower()
            and candidate_artifact == artifact
        )

        candidate_provenance_ok = False
        candidate_provenance_error = ""
        try:
            frozen_nw07 = nw07_export.load_canonical_config()
            expected_graph = nw07_export.verify_canonical_graph(
                frozen_nw07, repo_root=REPO_ROOT
            )
            pinned_inputs = nw07_export.verify_pinned_inputs(
                frozen_nw07, repo_root=REPO_ROOT
            )
            candidate_provenance_ok = (
                candidate.get("comparison_role") == "after_common_artifact"
                and candidate.get("performance_contract")
                == contract.PERF_CONTRACT_ID
                and candidate.get("performance_protocol")
                == contract.PERF_PROTOCOL_ID
                and candidate.get("profile_id") == common_contract.PROFILE_ID
                and candidate.get("requirements_digest")
                == common_contract.REQUIREMENTS_DIGEST
                and candidate.get("release_grade") is True
                and candidate.get("test_payload") is False
                and candidate_graph.get("path") == str(frozen_nw07["graph"])
                and candidate_graph.get("sha256")
                == expected_graph["graph_sha256"]
                and candidate_graph.get("ir_sha256")
                == expected_graph["ir_sha256"]
                and candidate_build_inputs.get("nw07_frozen_digest")
                == pinned_inputs["frozen_digest"]
                and candidate_build_inputs.get("observed_digest")
                == pinned_inputs["digest"]
                and candidate_build_inputs.get("matches_frozen_digest") is True
                and candidate_build_inputs.get("nw10_digest")
                == contract.build_inputs_digest(contract.compute_build_inputs())
                and candidate_dependency.get("matches_manifest") is True
                and isinstance(candidate_dependency.get("imports"), list)
                and candidate_runtime.get("status") == "passed"
                and candidate_runtime.get("exit_code") == 0
                and candidate_runtime.get("uncapped_present_verified") is True
            )
        except (KeyError, TypeError, ValueError, OSError) as exc:
            candidate_provenance_error = str(exc)

        def runs_valid(rows: Any) -> bool:
            return (
                isinstance(rows, list)
                and len(rows) == contract.PERF_RUN_COUNT
                and all(
                    isinstance(run, Mapping)
                    and run.get("status") == "passed"
                    and run.get("duration_seconds")
                    == contract.PERF_RUN_SECONDS
                    and _is_strict_under(
                        run.get("gpu_p99_ms"),
                        contract.FRAME_TIME_THRESHOLD_MS,
                    )
                    and _is_strict_under(
                        run.get("cpu_p99_ms"),
                        contract.FRAME_TIME_THRESHOLD_MS,
                    )
                    and isinstance(run.get("gpu_sample_count"), int)
                    and not isinstance(run.get("gpu_sample_count"), bool)
                    and run["gpu_sample_count"] >= 1
                    and isinstance(run.get("cpu_sample_count"), int)
                    and not isinstance(run.get("cpu_sample_count"), bool)
                    and run["cpu_sample_count"] >= 1
                    for run in rows
                )
            )

        runs_ok = runs_valid(runs)
        before_runs_ok = runs_valid(before.get("runs"))
        reference_environment_ok = (
            performance_reference_bound
            and performance.get("reference_environment") == expected_reference
            and str(performance.get("reference_binding_id", ""))
            == expected_binding_id
            and str(performance.get("reference_binding_digest", ""))
            == expected_binding_digest
        )
        before_matches_reference = (
            performance_reference_bound
            and before_artifact == expected_reference_artifact
        )
        comparison = performance.get("comparison")
        comparison = comparison if isinstance(comparison, Mapping) else {}
        package_identity_preserved = (
            comparison.get("same_canonical_package_sha256") is True
            and str(before_artifact.get("package_sha256", "")).lower()
            == str(artifact.get("package_sha256", "")).lower()
        )
        performance_details = {
            "contract": performance.get("performance_contract"),
            "protocol": performance.get("performance_protocol"),
            "reference_binding_id": performance.get("reference_binding_id"),
            "reference_binding_digest": performance.get(
                "reference_binding_digest"
            ),
            "expected_reference_binding_id": expected_binding_id or None,
            "reference_binding_status": reference_validation.get("status"),
            "reference_missing_bindings": list(
                reference_validation.get("missing_bindings") or []
            ),
            "reference_environment_matches": reference_environment_ok,
            "before_matches_frozen_reference_artifact": (
                before_matches_reference
            ),
            "candidate_provenance_verified": candidate_provenance_ok,
            "candidate_provenance_error": candidate_provenance_error or None,
            "candidate_artifact_matches_after": candidate_artifact == artifact,
            "same_canonical_package_sha256": package_identity_preserved,
            "expected_reference_artifact": expected_reference_artifact or None,
            "before_executable_sha256": before_artifact.get(
                "executable_sha256"
            ),
            "after_executable_sha256": artifact.get("executable_sha256"),
            "run_count": len(runs),
            "worst_gpu_p99_ms": after.get("worst_gpu_p99_ms"),
            "worst_cpu_submit_p99_ms": after.get("worst_cpu_submit_p99_ms"),
        }
        performance_ok = (
            performance_schema_ok
            and performance_reference_bound
            and reference_environment_ok
            and before_matches_reference
            and artifacts_ok
            and candidate_provenance_ok
            and package_identity_preserved
            and performance.get("status") == "verified_preserved"
            and performance.get("matched_environment") is True
            and performance.get("reasons") == []
            and performance.get("performance_contract")
            == contract.PERF_CONTRACT_ID
            and performance.get("performance_protocol")
            == contract.PERF_PROTOCOL_ID
            and comparison.get("faster_substitute_used") is False
            and runs_ok
            and before_runs_ok
            and _is_strict_under(
                before.get("worst_gpu_p99_ms"),
                contract.FRAME_TIME_THRESHOLD_MS,
            )
            and _is_strict_under(
                before.get("worst_cpu_submit_p99_ms"),
                contract.FRAME_TIME_THRESHOLD_MS,
            )
            and _is_strict_under(
                after.get("worst_gpu_p99_ms"),
                contract.FRAME_TIME_THRESHOLD_MS,
            )
            and _is_strict_under(
                after.get("worst_cpu_submit_p99_ms"),
                contract.FRAME_TIME_THRESHOLD_MS,
            )
        )
    else:
        performance_schema_error = "not provided"
    performance_verification = _verification(
        "verified"
        if performance_ok
        else (
            "failed"
            if performance_reference_bound
            and performance is not None
            and performance.get("status") in {"verified_preserved", "failed"}
            else "pending"
        ),
        (
            "nw07_perf_ref_v1 matched performance gates are verified"
            if performance_ok
            else (
                "matched nw07_perf_ref_v1 performance preservation is pending"
                if not performance_reference_bound
                else "performance record does not match the frozen reference"
            )
        ),
        schema_error=performance_schema_error or None,
        **performance_details,
    )

    correctness_schema_error = "not provided"
    visual_ok = False
    visual_details: dict[str, Any] = {}
    retained_evidence_present = False
    tier_evidence_ok = False
    if correctness is not None:
        correctness_schema_ok, correctness_schema_error = (
            _validate_evidence_schema(
                correctness,
                "nw10-common-artifact-correctness.schema.json",
            )
        )
        correctness_common = correctness.get("common_artifact")
        correctness_common = (
            correctness_common
            if isinstance(correctness_common, Mapping)
            else {}
        )
        comparison = correctness.get("comparison")
        comparison = comparison if isinstance(comparison, Mapping) else {}
        packed = correctness.get("packed_unpacked_comparison")
        packed = packed if isinstance(packed, Mapping) else {}
        packed_runtime = packed.get("runtime")
        packed_runtime = (
            packed_runtime if isinstance(packed_runtime, Mapping) else {}
        )
        declared_codec_id = str(packed.get("codec_id", ""))
        declared_layout_id = str(packed.get("layout_id", ""))
        intro_codec_id = str(compact_intro.get("codec_id", ""))
        intro_layout_id = str(compact_intro.get("layout_id", ""))
        codec_layout_ok = (
            declared_codec_id in contract.PLAYER_SUPPORTED_CODEC_IDS
            and declared_layout_id in contract.KNOWN_LAYOUT_IDS
            and intro_codec_id == declared_codec_id
            and intro_layout_id == declared_layout_id
            and compact_ledger_identity_ok
        )
        def hash64(value: Any) -> bool:
            return re.fullmatch(
                r"[0-9a-f]{64}", str(value or "").lower()
            ) is not None

        tier_hash_bindings_ok = True
        tier_hashes: dict[str, dict[str, Any]] = {}
        for tier in ("ada", "blackwell"):
            runtime = packed_runtime.get(tier)
            runtime = runtime if isinstance(runtime, Mapping) else {}
            tier_record = correctness.get(tier)
            tier_record = (
                tier_record if isinstance(tier_record, Mapping) else {}
            )
            tier_artifact = tier_record.get("artifact")
            tier_artifact = (
                tier_artifact if isinstance(tier_artifact, Mapping) else {}
            )
            unpacked_sha = str(
                runtime.get("unpacked_executable_sha256", "")
            ).lower()
            packed_sha = str(
                runtime.get("packed_executable_sha256", "")
            ).lower()
            encoded_sha = str(
                runtime.get("encoded_container_sha256", "")
            ).lower()
            encoded_bytes = runtime.get("encoded_container_bytes")
            unpacked_manifest_sha = str(
                runtime.get("unpacked_manifest_sha256", "")
            ).lower()
            packed_manifest_sha = str(
                runtime.get("packed_manifest_sha256", "")
            ).lower()
            tier_hashes[tier] = {
                "unpacked": unpacked_sha,
                "packed": packed_sha,
                "encoded": encoded_sha,
                "encoded_bytes": encoded_bytes,
                "unpacked_manifest": unpacked_manifest_sha,
                "packed_manifest": packed_manifest_sha,
            }
            tier_hash_bindings_ok = tier_hash_bindings_ok and (
                runtime.get("status") == "passed"
                and hash64(unpacked_sha)
                and hash64(packed_sha)
                and hash64(encoded_sha)
                and _is_positive_int(encoded_bytes)
                and hash64(unpacked_manifest_sha)
                and hash64(packed_manifest_sha)
                and unpacked_sha != packed_sha
                and unpacked_sha != validated_sha
                and packed_sha != validated_sha
                and str(
                    tier_artifact.get("unpacked_executable_sha256", "")
                ).lower()
                == unpacked_sha
                and str(
                    tier_artifact.get("packed_executable_sha256", "")
                ).lower()
                == packed_sha
                and str(
                    tier_artifact.get("encoded_container_sha256", "")
                ).lower()
                == encoded_sha
                and tier_artifact.get("encoded_container_bytes")
                == encoded_bytes
                and str(
                    tier_artifact.get("unpacked_manifest_sha256", "")
                ).lower()
                == unpacked_manifest_sha
                and str(
                    tier_artifact.get("packed_manifest_sha256", "")
                ).lower()
                == packed_manifest_sha
                and str(runtime.get("codec_id", "")) == declared_codec_id
                and str(runtime.get("layout_id", "")) == declared_layout_id
                and int(runtime.get("frames_per_tick", -1)) == 1
                and int(runtime.get("present_sync_interval", -1)) == 0
            )
        cross_tier_hashes_ok = all(
            len({values[key] for values in tier_hashes.values()}) == 1
            for key in (
                "unpacked",
                "packed",
                "encoded",
                "encoded_bytes",
                "unpacked_manifest",
                "packed_manifest",
            )
        )
        tier_hash_bindings_ok = tier_hash_bindings_ok and cross_tier_hashes_ok
        evidence_entries = (
            correctness_evidence
            if isinstance(correctness_evidence, Mapping)
            else {}
        )
        retained_evidence_present = True
        tier_evidence_ok = True
        header_codec_layout_ok = True
        actual_encoded_shas: set[str] = set()
        actual_encoded_sizes: set[int] = set()
        retained_manifest_binding_ok = True
        for tier, hashes in tier_hashes.items():
            tier_entry = evidence_entries.get(tier)
            tier_entry = (
                tier_entry if isinstance(tier_entry, Mapping) else {}
            )
            for mode in ("unpacked", "packed"):
                entry = tier_entry.get(mode)
                if entry is None:
                    retained_evidence_present = False
                    tier_evidence_ok = False
                    continue
                try:
                    data = _evidence_bytes(entry)
                    payload, embedded_manifest = (
                        nw05_export.extract_embedded_package(data)
                    )
                except Exception:  # noqa: BLE001 - invalid evidence fails the gate
                    tier_evidence_ok = False
                    continue
                if hashlib.sha256(data).hexdigest() != hashes[mode]:
                    tier_evidence_ok = False
                    continue
                embedded_manifest_sha = hashlib.sha256(
                    embedded_manifest
                ).hexdigest()
                expected_diagnostic_manifest_sha = hashes[
                    f"{mode}_manifest"
                ]
                if (
                    not hash64(expected_diagnostic_manifest_sha)
                    or embedded_manifest_sha
                    != expected_diagnostic_manifest_sha
                ):
                    retained_manifest_binding_ok = False
                    tier_evidence_ok = False
                    continue
                payload_sha = hashlib.sha256(payload).hexdigest()
                if mode == "unpacked":
                    if compact.is_compact_container(payload):
                        tier_evidence_ok = False
                        continue
                    if payload_sha != canonical_package_sha:
                        tier_evidence_ok = False
                    if canonical_package_bytes is not None and len(
                        payload
                    ) != int(canonical_package_bytes):
                        tier_evidence_ok = False
                else:
                    if not compact.is_compact_container(payload):
                        tier_evidence_ok = False
                        continue
                    try:
                        header = compact.parse_header(payload)
                    except Exception:  # noqa: BLE001
                        tier_evidence_ok = False
                        continue
                    if (
                        str(header.get("codec_id", ""))
                        != declared_codec_id
                        or str(header.get("layout_id", ""))
                        != declared_layout_id
                    ):
                        header_codec_layout_ok = False
                        tier_evidence_ok = False
                        continue
                    actual_encoded_shas.add(payload_sha)
                    actual_encoded_sizes.add(len(payload))
                    if (
                        payload_sha != hashes["encoded"]
                        or len(payload) != hashes["encoded_bytes"]
                    ):
                        tier_evidence_ok = False
                        continue
                    try:
                        decoded = compact.decode_compact(payload)
                    except Exception:  # noqa: BLE001
                        tier_evidence_ok = False
                        continue
                    if (
                        hashlib.sha256(decoded).hexdigest()
                        != canonical_package_sha
                    ):
                        tier_evidence_ok = False
                    if canonical_package_bytes is not None and len(
                        decoded
                    ) != int(canonical_package_bytes):
                        tier_evidence_ok = False
        actual_encoded_sha = (
            next(iter(actual_encoded_shas))
            if len(actual_encoded_shas) == 1
            else ""
        )
        actual_encoded_bytes = (
            next(iter(actual_encoded_sizes))
            if len(actual_encoded_sizes) == 1
            else None
        )
        encoded_payload_identity_ok = (
            retained_evidence_present
            and len(actual_encoded_shas) == 1
            and len(actual_encoded_sizes) == 1
            and actual_encoded_sha == compact_encoded_sha
            and actual_encoded_bytes == compact_encoded_bytes
            and compact_ledger_identity_ok
        )
        pe_payload_identity_ok = True
        if (
            pe_ledger is not None
            and pe_ledger.get("status") == "verified"
            and validated_mode == "compact"
        ):
            pe_final = pe_ledger.get("final_artifact")
            pe_final = pe_final if isinstance(pe_final, Mapping) else {}
            pe_payload_identity_ok = (
                str(pe_final.get("embedded_payload_sha256", "")).lower()
                == actual_encoded_sha
                and pe_final.get("embedded_payload_bytes")
                == actual_encoded_bytes
            )
        tier_evidence_ok = (
            tier_evidence_ok
            and encoded_payload_identity_ok
            and retained_manifest_binding_ok
            and pe_payload_identity_ok
        )
        codec_layout_ok = (
            codec_layout_ok
            and header_codec_layout_ok
            and encoded_payload_identity_ok
        )
        packed_ok = (
            packed.get("status") == "verified"
            and _is_finite_nonnegative(packed.get("worst_absolute_error"))
            and float(packed["worst_absolute_error"])
            <= contract.IMAGE_ABSOLUTE_TOLERANCE
            and str(packed.get("packed_package_sha256", "")).lower()
            == canonical_package_sha
            and str(packed.get("unpacked_package_sha256", "")).lower()
            == canonical_package_sha
            and tier_hash_bindings_ok
            and retained_evidence_present
            and tier_evidence_ok
            and codec_layout_ok
        )
        visual_ok = (
            correctness_schema_ok
            and correctness.get("status") == "verified"
            and str(correctness.get("source_head_sha", "")) == head_sha
            and str(
                (correctness.get("source") or {}).get(
                    "implementation_head", ""
                )
            )
            == head_sha
            and str(correctness_common.get("package_sha256", "")).lower()
            == canonical_package_sha
            and bool(canonical_artifact_key)
            and str(correctness_common.get("artifact_key", ""))
            == canonical_artifact_key
            and str(correctness_common.get("profile_id", ""))
            == common_contract.PROFILE_ID
            and str(correctness_common.get("requirements_digest", ""))
            == common_contract.REQUIREMENTS_DIGEST
            and bool(canonical_manifest_sha)
            and _manifest_sha(
                correctness, "common_artifact", "manifest_sha256"
            )
            == canonical_manifest_sha
            and str(
                correctness_common.get("final_executable_sha256", "")
            ).lower()
            == validated_sha
            and comparison.get("absolute_tolerance")
            == contract.IMAGE_ABSOLUTE_TOLERANCE
            and comparison.get("relative_tolerance")
            == contract.IMAGE_RELATIVE_TOLERANCE
            and comparison.get("absolute_required") is True
            and _is_finite_nonnegative(
                comparison.get("worst_absolute_error")
            )
            and float(comparison["worst_absolute_error"])
            <= contract.IMAGE_ABSOLUTE_TOLERANCE
            and _is_finite_nonnegative(
                comparison.get("worst_relative_error")
            )
            and float(comparison["worst_relative_error"])
            <= contract.IMAGE_RELATIVE_TOLERANCE
            and packed_ok
        )
        visual_details = {
            "correctness_head": correctness.get("source_head_sha"),
            "final_executable_sha256": correctness_common.get(
                "final_executable_sha256"
            ),
            "worst_absolute_error": comparison.get("worst_absolute_error"),
            "packed_worst_absolute_error": packed.get("worst_absolute_error"),
            "tier_hash_bindings_verified": tier_hash_bindings_ok,
            "cross_tier_artifact_identity_verified": cross_tier_hashes_ok,
            "actual_encoded_package_sha256": actual_encoded_sha or None,
            "actual_encoded_package_bytes": actual_encoded_bytes,
            "intro_encoded_package_sha256": compact_encoded_sha or None,
            "intro_encoded_package_bytes": compact_encoded_bytes,
            "compact_ledger_identity_applicable": (
                compact_ledger_identity_applicable
            ),
            "compact_ledger_encoded_payload_mode": (
                compact_encoded_payload_mode
            ),
            "compact_ledger_identity_verified": compact_ledger_identity_ok,
            "pe_payload_identity_verified": pe_payload_identity_ok,
            "retained_manifest_binding_verified": retained_manifest_binding_ok,
            "declared_codec_id": declared_codec_id or None,
            "declared_layout_id": declared_layout_id or None,
            "intro_codec_id": intro_codec_id or None,
            "intro_layout_id": intro_layout_id or None,
            "codec_layout_identity_verified": codec_layout_ok,
            "retained_diagnostic_evidence_present": (
                retained_evidence_present
            ),
            "retained_diagnostic_evidence_verified": tier_evidence_ok,
        }
    visual_verification = _verification(
        "verified"
        if visual_ok
        else (
            "pending"
            if correctness is not None
            and correctness.get("status") == "verified"
            and not retained_evidence_present
            else (
                "failed"
                if correctness is not None
                and correctness.get("status") == "verified"
                else "pending"
            )
        ),
        (
            "packed/unpacked visual validation is verified"
            if visual_ok
            else "packed/unpacked physical visual validation is pending"
        ),
        schema_error=correctness_schema_error or None,
        **visual_details,
        tolerance=contract.image_tolerances(),
    )

    certification = {
        "status": "pending",
        "decoded_package_identity": decoded_identity,
        "all_required_bytes_accounted": bytes_verification,
        "dependency_closure": dependency_verification,
        "pe_ledger_binding": pe_verification,
        "clean_system": clean_verification,
        "startup": startup_verification,
        "memory": memory_verification,
        "performance": performance_verification,
        "visual": visual_verification,
    }
    statuses = [
        value["status"]
        for value in certification.values()
        if isinstance(value, Mapping) and "status" in value
    ]
    certification["status"] = (
        "failed"
        if "failed" in statuses
        else ("verified" if statuses and all(item == "verified" for item in statuses) else "pending")
    )
    certification["failed_gates"] = [
        name
        for name, value in certification.items()
        if isinstance(value, Mapping)
        and value.get("status") == "failed"
    ]
    certification["pending_gates"] = [
        name
        for name, value in certification.items()
        if isinstance(value, Mapping)
        and value.get("status") == "pending"
    ]
    return certification


def build_feasibility(
    *,
    outcome: Mapping[str, Any],
    proof: Mapping[str, Any],
    intro: Mapping[str, Any],
    run_id: int,
    head_sha: str,
    artifact_name: str = "nw10-windows-compact-evidence",
    artifact_id: int | None = None,
    artifact_digest: str = "",
    prior_valid_ledger: Mapping[str, Any] | None = None,
    prior_valid_ledger_path: str | None = None,
    pe_ledger: Mapping[str, Any] | None = None,
    dependency_closure: Mapping[str, Any] | None = None,
    system_validation: Mapping[str, Any] | None = None,
    performance: Mapping[str, Any] | None = None,
    correctness: Mapping[str, Any] | None = None,
    correctness_evidence: Mapping[str, Any] | None = None,
    perf_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if proof.get("test_payload") is True or intro.get("test_payload") is True:
        raise ValueError("feasibility publication requires release-grade reports")
    if perf_contract is None:
        perf_contract = json.loads(
            PERF_REFERENCE_PATH.read_text(encoding="utf-8")
        )
    if proof.get("release_grade") is not True or intro.get("release_grade") is not True:
        raise ValueError("feasibility publication requires release-grade reports")
    proof_kind = str(proof.get("measurement_kind") or "")
    if proof_kind not in {
        "release_decoder_off",
        "release_decoder_off_size_blocked",
    }:
        raise ValueError(
            "proof report is not a measured release decoder-OFF export or "
            "decoder-OFF size-blocked measurement"
        )
    current_digest = contract.build_inputs_digest(contract.compute_build_inputs())
    if proof.get("nw10_build_inputs_digest") != current_digest:
        raise ValueError(
            "proof report build inputs do not match the checked-out implementation"
        )
    if intro.get("nw10_build_inputs_digest") != current_digest:
        raise ValueError(
            "intro report build inputs do not match the checked-out implementation"
        )
    templates = proof.get("templates")
    if not isinstance(templates, Mapping) or templates.get(
        "canonical_decoder_off"
    ) is not True:
        raise ValueError(
            "proof report does not prove a decoder-OFF canonical baseline"
        )
    canonical_intro, compact_intro = _intro_artifacts(intro)

    if proof_kind == "release_decoder_off":
        proof_artifact = _proof_artifact(proof)
        proof_bytes = int(proof_artifact["executable_bytes"])
    else:
        if proof.get("success") is not False:
            raise ValueError("size-blocked proof report must record success=false")
        if proof.get("measurement_outcome") != "size_target_not_met":
            raise ValueError(
                "size-blocked proof report does not identify the proof-tier blocker"
            )
        measured = proof.get("measured_distribution_bytes")
        if measured is None or int(measured) <= contract.PROOF_TIER_BYTES:
            raise ValueError(
                "size-blocked proof report does not retain an above-tier measurement"
            )
        proof_bytes = int(measured)
    proof_within = proof_bytes <= contract.PROOF_TIER_BYTES

    measured_candidate = outcome.get("minimum_measured_candidate_bytes")
    validated_bytes = outcome.get("minimum_validated_runnable_bytes")
    outcome_validated_sha = str(
        outcome.get("validated_executable_sha256") or ""
    ).lower()
    validated_mode = str(outcome.get("validated_mode") or "none")
    runtime_status = str(
        outcome.get("compact_candidate_runtime_status") or "pending_runtime_validation"
    )
    candidate_exit = _windows_exit_code(outcome.get("compact_candidate_exit_code"))
    canonical_exit = _windows_exit_code(outcome.get("canonical_fallback_exit_code"))
    intro_candidate = outcome.get("intro_compact_candidate_bytes")
    proof_candidate = outcome.get("proof_compact_candidate_bytes")
    candidate_size = int(compact_intro.get("executable_bytes", 0))
    candidate_sha = str(compact_intro.get("executable_sha256", "")).lower()
    canonical_size = int(canonical_intro.get("executable_bytes", 0))
    canonical_sha = str(
        canonical_intro.get("executable_sha256", "")
    ).lower()
    if candidate_size <= 0:
        raise ValueError("intro compact candidate has no positive executable size")
    if re.fullmatch(r"[0-9a-f]{64}", candidate_sha) is None:
        raise ValueError("intro compact candidate executable SHA-256 is malformed")
    if canonical_size <= 0:
        raise ValueError("intro canonical artifact has no positive executable size")
    if re.fullmatch(r"[0-9a-f]{64}", canonical_sha) is None:
        raise ValueError("intro canonical artifact SHA-256 is malformed")
    if runtime_status == "validated_runnable":
        if validated_mode != "compact" or validated_bytes is None:
            raise ValueError(
                "validated compact runtime status requires validated_mode=compact"
            )
        if intro_candidate is not None and int(intro_candidate) != candidate_size:
            raise ValueError(
                "outcome intro compact candidate size does not match the intro report"
            )
        if int(validated_bytes) != candidate_size:
            raise ValueError(
                "validated compact byte count does not match the intro candidate"
            )
        if outcome_validated_sha != candidate_sha:
            raise ValueError(
                "validated compact executable SHA-256 does not match the intro candidate"
            )
        if compact_intro.get("selected_mode") != "compact":
            raise ValueError("validated compact runtime is not selected in the intro report")
        if int(compact_intro.get("selected_executable_bytes", -1)) != candidate_size:
            raise ValueError(
                "selected intro compact byte count does not match the validated candidate"
            )
        if str(compact_intro.get("selected_executable_sha256", "")).lower() != candidate_sha:
            raise ValueError(
                "selected intro compact SHA-256 does not match the validated candidate"
            )
    if validated_mode == "canonical_fallback":
        if validated_bytes is None or canonical_exit != 0:
            raise ValueError(
                "validated canonical fallback requires a zero exit code and bytes"
            )
        if int(validated_bytes) != canonical_size:
            raise ValueError(
                "validated canonical byte count does not match the intro artifact"
            )
        if outcome_validated_sha != canonical_sha:
            raise ValueError(
                "validated canonical executable SHA-256 does not match the intro artifact"
            )
        if compact_intro.get("selected_mode") != "canonical":
            raise ValueError(
                "validated canonical fallback is not selected in the intro report"
            )
        if (
            int(compact_intro.get("selected_executable_bytes", -1))
            != canonical_size
        ):
            raise ValueError(
                "selected intro canonical byte count does not match the validated artifact"
            )
        if (
            str(compact_intro.get("selected_executable_sha256", "")).lower()
            != canonical_sha
        ):
            raise ValueError(
                "selected intro canonical SHA-256 does not match the validated artifact"
            )
    if validated_mode == "compact":
        validated_artifact_sha = candidate_sha
        validated_artifact_size = candidate_size
        validated_ready = (
            runtime_status == "validated_runnable"
            and candidate_exit == 0
            and validated_bytes is not None
            and int(validated_bytes) == candidate_size
            and outcome_validated_sha == candidate_sha
        )
    elif validated_mode == "canonical_fallback":
        validated_artifact_sha = canonical_sha
        validated_artifact_size = canonical_size
        validated_ready = (
            canonical_exit == 0
            and validated_bytes is not None
            and int(validated_bytes) == canonical_size
            and outcome_validated_sha == canonical_sha
        )
    else:
        validated_artifact_sha = ""
        validated_artifact_size = 0
        validated_ready = False
    base_target_met = bool(
        proof_within
        and validated_ready
        and validated_artifact_size <= contract.COMPACT_TARGET_BYTES
    )
    certification = _certify_compact_candidate(
        intro=intro,
        validated_mode=validated_mode,
        validated_sha=validated_artifact_sha,
        validated_size=validated_artifact_size,
        head_sha=head_sha,
        pe_ledger=pe_ledger,
        dependency_closure=dependency_closure,
        system_validation=system_validation,
        performance=performance,
        correctness=correctness,
        correctness_evidence=correctness_evidence,
        perf_contract=perf_contract,
    )
    target_met = bool(
        base_target_met
        and certification.get("status") == "verified"
    )
    true_64k = target_met
    canonical_runtime = (
        "validated_runnable"
        if validated_bytes is not None
        and validated_mode == "canonical_fallback"
        and canonical_exit == 0
        else "pending_runtime_validation"
    )
    hosted_noncertifying = (
        validated_mode == "canonical_fallback_pending_physical"
        and canonical_exit not in (None, 0)
    )
    if runtime_status == "validated_runnable":
        failure_classification = "none"
    elif hosted_noncertifying:
        failure_classification = "non_certifying_hosted_failure"
    elif runtime_status == "rejected_runtime_failure" and candidate_exit == 70:
        failure_classification = "embedded_package_invalid"
    elif runtime_status == "rejected_runtime_failure":
        failure_classification = "runtime_failure"
    else:
        failure_classification = "runtime_validation_missing"

    compact: dict[str, Any] = {
        "bytes": candidate_size,
        "sha256": candidate_sha,
        "runtime_status": (
            runtime_status
            if runtime_status
            in {
                "validated_runnable",
                "rejected_runtime_failure",
                "pending_runtime_validation",
            }
            else "pending_runtime_validation"
        ),
        "exit_code": (
            0 if runtime_status == "validated_runnable" else candidate_exit
        ),
        "exit_code_hex": _windows_exit_hex(
            0 if runtime_status == "validated_runnable" else candidate_exit
        ),
        "failure_classification": failure_classification,
        "counted_as_validated_runnable": runtime_status == "validated_runnable",
    }

    source: dict[str, Any] = {
        "kind": "github_actions_windows_runtime",
        "workflow": "NW-10 Compact Export and Restricted Intro",
        "run_id": run_id,
        "head_sha": head_sha,
        "artifact_name": artifact_name,
        "measurement_tool": "tools/nw10_feasibility.py",
    }
    if artifact_id is not None:
        source["artifact_id"] = int(artifact_id)
    normalized_artifact_digest = _normalize_artifact_digest(artifact_digest)
    if normalized_artifact_digest:
        source["artifact_digest"] = normalized_artifact_digest

    record: dict[str, Any] = {
        "format": FORMAT,
        "version": 1,
        "task": "NW-10",
        "implementation_base": contract.IMPLEMENTATION_BASE,
        "target_bytes": contract.COMPACT_TARGET_BYTES,
        "target_met": target_met,
        "classification": "complete" if target_met else "continue",
        "universal_64k_claim": False,
        "source": source,
        "measurements": {
            "proof_measured_bytes": proof_bytes,
            "proof_measurement_status": (
                "measured_decoder_off"
                if proof_kind == "release_decoder_off"
                else "measured_decoder_off_size_blocked"
            ),
            "canonical_proof_within_tier": proof_within,
            "proof_build_inputs_digest": proof.get("nw10_build_inputs_digest"),
            "minimum_measured_candidate_bytes": (
                int(measured_candidate) if measured_candidate is not None else None
            ),
            "minimum_validated_runnable_bytes": (
                int(validated_bytes) if validated_bytes is not None else None
            ),
            "validated_mode": (
                validated_mode
                if validated_mode
                in {
                    "compact",
                    "canonical_fallback",
                    "canonical_fallback_pending_physical",
                    "pending_runtime_validation",
                    "none",
                }
                else "none"
            ),
            "validated_artifact_bytes": (
                validated_artifact_size if validated_artifact_sha else None
            ),
            "validated_artifact_sha256": (
                validated_artifact_sha or None
            ),
            "validated_payload_kind": (
                "compact_container"
                if validated_mode == "compact"
                else (
                    "canonical_package"
                    if validated_mode == "canonical_fallback"
                    else None
                )
            ),
        },
        "compact_candidate": compact,
        "validated_artifact": (
            {
                "bytes": validated_artifact_size,
                "sha256": validated_artifact_sha,
                "mode": validated_mode,
                "payload_kind": (
                    "compact_container"
                    if validated_mode == "compact"
                    else (
                        "canonical_package"
                        if validated_mode == "canonical_fallback"
                        else None
                    )
                ),
            }
            if validated_artifact_sha
            else None
        ),
        "certification": certification,
        "canonical_fallback": {
            "bytes": int(canonical_intro["executable_bytes"]),
            "sha256": str(canonical_intro["executable_sha256"]).lower(),
            "runtime_status": canonical_runtime,
            "exit_code": canonical_exit,
            "exit_code_hex": _windows_exit_hex(canonical_exit),
            "link_manifest_verified": True,
            "present_sync_interval": 0,
            "dependency_closure": {
                "closure_verified": (
                    validated_mode == "canonical_fallback"
                    and certification["dependency_closure"].get("status")
                    == "verified"
                ),
                "sidecar_files": (
                    list(dependency_closure.get("sidecar_files") or [])
                    if validated_mode == "canonical_fallback"
                    and dependency_closure is not None
                    else []
                ),
                "canonical_imports": (
                    list(dependency_closure.get("canonical_imports") or [])
                    if validated_mode == "canonical_fallback"
                    and dependency_closure is not None
                    else _canonical_dependencies(intro)
                ),
            },
        },
        "acceptance_linkage": {
            "byte_ledger": "benchmarks/nw10/compact-proof.v1.json",
            "dominant_costs_source": "benchmarks/nw10/compact-proof.v1.json#dominant_costs",
            "tested_alternatives_source": "benchmarks/nw10/compact-proof.v1.json#alternatives",
            "decision": "complete" if target_met else "continue",
            "true_64k_artifact_delivered": true_64k,
        },
        "privacy": {
            "raw_stdout_stderr_included": False,
            "runner_paths_included": False,
            "user_profile_paths_included": False,
            "whitelist_generated": True,
        },
    }
    blockers: list[dict[str, Any]] = []
    if not proof_within:
        blockers.append(
            {
                "id": "canonical_proof_tier",
                "status": "measured_above_tier",
                "exit_code": None,
                "exit_code_hex": None,
                "measured_bytes": proof_bytes,
                "reason": (
                    "The decoder-OFF canonical proof exceeds the frozen "
                    "262,144-byte proof tier; the exact measured size is retained "
                    "as a tested blocker and the decision remains continue."
                ),
            }
        )
    if not target_met:
        if base_target_met and certification.get("status") != "verified":
            blockers.append(
                {
                    "id": "compact_certification_evidence",
                    "status": (
                        "rejected"
                        if certification.get("status") == "failed"
                        else "pending"
                    ),
                    "exit_code": None,
                    "exit_code_hex": None,
                    "measured_bytes": (
                        validated_artifact_size
                        if validated_ready
                        else candidate_size
                    ),
                    "reason": (
                        "The candidate meets the byte target and reports a "
                        "validated runtime, but clean-target startup/memory/"
                        "clean-system evidence, dependency closure, decoded "
                        "package identity, attributed decoder bytes, matched "
                        "nw07_perf_ref_v1 performance gates, or visual evidence "
                        "is not verified. Pending gates: "
                        + ", ".join(certification.get("pending_gates") or [])
                        + "; failed gates: "
                        + ", ".join(certification.get("failed_gates") or [])
                    ),
                }
            )
        elif validated_ready and validated_artifact_size > contract.COMPACT_TARGET_BYTES:
            blockers.append(
                {
                    "id": "validated_artifact_target",
                    "status": "measured_above_target",
                    "exit_code": 0,
                    "exit_code_hex": _windows_exit_hex(0),
                    "measured_bytes": validated_artifact_size,
                    "reason": (
                        "The validated "
                        f"{validated_mode} single-file artifact is "
                        f"{validated_artifact_size} bytes, above the 65,536-byte "
                        "target; the exact measurement is retained and the "
                        "decision remains continue."
                    ),
                }
            )
        elif hosted_noncertifying:
            blocker_reason = (
                "The hosted Windows runner could not certify either the compact "
                "candidate or the canonical fallback; the compact result remains "
                "pending physical runtime validation rather than being attributed "
                "to compact packaging."
            )
            blocker_status = "pending_physical"
            blockers.append(
                {
                    "id": "compact_candidate_runtime",
                    "status": blocker_status,
                    "exit_code": candidate_exit,
                    "exit_code_hex": _windows_exit_hex(candidate_exit),
                    "measured_bytes": candidate_size,
                    "reason": blocker_reason,
                }
            )
        else:
            blocker_reason = (
                "The measured compact candidate did not produce a validated "
                "at-most-65536-byte runnable artifact."
            )
            blocker_status = (
                "rejected" if runtime_status == "rejected_runtime_failure" else "pending"
            )
            blockers.append(
                {
                    "id": "compact_candidate_runtime",
                    "status": blocker_status,
                    "exit_code": candidate_exit,
                    "exit_code_hex": _windows_exit_hex(candidate_exit),
                    "measured_bytes": candidate_size,
                    "reason": blocker_reason,
                }
            )
    if blockers:
        record["blocker"] = blockers[0]
        record["blockers"] = blockers

    if prior_valid_ledger is not None:
        if prior_valid_ledger_path is None:
            raise ValueError(
                "prior valid ledger provenance path is required with prior_valid_ledger"
            )
        prior_final, _prior_conclusion, prior_head = _validate_prior_valid_ledger(
            prior_valid_ledger,
            provenance_path=prior_valid_ledger_path,
        )
        prior_sha = str(prior_final["executable_sha256"]).lower()
        prior_bytes = int(prior_final["executable_bytes"])
        record["prior_valid_artifact"] = {
            "executable_bytes": prior_bytes,
            "executable_sha256": prior_sha,
            "runtime_status": "validated_runnable",
            "status": "archived_prior_valid_fallback",
            "source_head_sha": prior_head,
            "reason": (
                "The current measurement is not yet a validated runnable "
                "replacement, so the last complete runnable ledger remains "
                "available without being represented as current-head evidence."
            ),
        }
        if not target_met and validated_bytes is None:
            record["selected_artifact"] = {
                "bytes": prior_bytes,
                "sha256": prior_sha,
                "runtime_status": "validated_runnable",
                "source": "prior_valid_artifact",
                "ledger": prior_valid_ledger_path,
                "reason": (
                    "No current artifact has completed runtime validation; "
                    "select the last validated runnable distribution until "
                    "new physical evidence is published."
                ),
            }

    if "selected_artifact" not in record:
        if validated_bytes is not None and validated_mode == "compact":
            selected_sha = candidate_sha
            selected_bytes = int(validated_bytes)
            selected_source = "compact_candidate"
        elif validated_bytes is not None and validated_mode == "canonical_fallback":
            selected_sha = str(canonical_intro["executable_sha256"]).lower()
            selected_bytes = int(validated_bytes)
            selected_source = "canonical_fallback"
        else:
            selected_sha = str(canonical_intro["executable_sha256"]).lower()
            selected_bytes = int(canonical_intro["executable_bytes"])
            selected_source = "pending_current_candidate"
        record["selected_artifact"] = {
            "bytes": selected_bytes,
            "sha256": selected_sha,
            "runtime_status": (
                "validated_runnable"
                if validated_bytes is not None
                else "pending_runtime_validation"
            ),
            "source": selected_source,
            "ledger": "build/nw10-windows-pe-ledger.json",
            "reason": (
                "Selection is derived from the current measured feasibility "
                "outcome and is not promoted beyond its runtime validation state."
            ),
        }
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outcome", type=Path, required=True)
    parser.add_argument("--proof-report", type=Path, required=True)
    parser.add_argument("--intro-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", type=int, default=int(os.environ.get("GITHUB_RUN_ID", "1")))
    parser.add_argument("--head-sha", default=os.environ.get("GITHUB_SHA", "0" * 40))
    parser.add_argument("--artifact-name", default="nw10-windows-compact-evidence")
    parser.add_argument("--artifact-id", type=int)
    parser.add_argument("--artifact-digest", default="")
    parser.add_argument("--prior-valid-ledger", type=Path)
    parser.add_argument("--pe-ledger", type=Path)
    parser.add_argument("--dependency-closure", type=Path)
    parser.add_argument("--system-validation", type=Path)
    parser.add_argument("--performance-preservation", type=Path)
    parser.add_argument("--correctness", type=Path)
    parser.add_argument(
        "--correctness-ada-unpacked-executable", type=Path
    )
    parser.add_argument("--correctness-ada-packed-executable", type=Path)
    parser.add_argument(
        "--correctness-blackwell-unpacked-executable", type=Path
    )
    parser.add_argument(
        "--correctness-blackwell-packed-executable", type=Path
    )
    args = parser.parse_args(argv)
    if not (
        len(args.head_sha) == 40
        and all(character in "0123456789abcdef" for character in args.head_sha)
    ):
        print("head SHA is not a 40-character lowercase hex digest", file=sys.stderr)
        return 1
    prior_valid_ledger = (
        _load(args.prior_valid_ledger)
        if args.prior_valid_ledger is not None
        else None
    )
    prior_valid_ledger_path = (
        _repo_relative_path(args.prior_valid_ledger)
        if args.prior_valid_ledger is not None
        else None
    )
    correctness_evidence: dict[str, dict[str, Path]] = {}
    for tier, mode, value in (
        ("ada", "unpacked", args.correctness_ada_unpacked_executable),
        ("ada", "packed", args.correctness_ada_packed_executable),
        (
            "blackwell",
            "unpacked",
            args.correctness_blackwell_unpacked_executable,
        ),
        (
            "blackwell",
            "packed",
            args.correctness_blackwell_packed_executable,
        ),
    ):
        if value is not None:
            correctness_evidence.setdefault(tier, {})[mode] = value
    record = build_feasibility(
        outcome=_load(args.outcome),
        proof=_load(args.proof_report),
        intro=_load(args.intro_report),
        run_id=args.run_id,
        head_sha=args.head_sha,
        artifact_name=args.artifact_name,
        artifact_id=args.artifact_id,
        artifact_digest=args.artifact_digest,
        prior_valid_ledger=prior_valid_ledger,
        prior_valid_ledger_path=prior_valid_ledger_path,
        pe_ledger=_load(args.pe_ledger) if args.pe_ledger is not None else None,
        dependency_closure=(
            _load(args.dependency_closure)
            if args.dependency_closure is not None
            else None
        ),
        system_validation=(
            _load(args.system_validation)
            if args.system_validation is not None
            else None
        ),
        performance=(
            _load(args.performance_preservation)
            if args.performance_preservation is not None
            else None
        ),
        correctness=(
            _load(args.correctness) if args.correctness is not None else None
        ),
        correctness_evidence=correctness_evidence or None,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    validate_file_against_schema(
        args.output,
        REPO_ROOT / "schemas" / "nw10-windows-compact-feasibility.schema.json",
    )
    print(json.dumps({"status": "valid", "target_met": record["target_met"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
