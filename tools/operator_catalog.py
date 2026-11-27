#!/usr/bin/env python3
"""Pinned Werkkzeug3/Werkkzeug4 operator catalog regeneration and review checks.

The raw Farbrausch submodule is intentionally not checked out in this worktree.
The pinned input is the checked-in declaration export captured from the exact
submodule commit; regeneration is deterministic from that pinned export.  No
Farbrausch source code or assets are copied into nodewerk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


CATALOG_DIR = Path("references") / "operator_catalog"
SOURCES = "pinned-sources.v1.json"
DECLARATIONS = {
    "werkkzeug3": "werkkzeug3.declarations.v1.json",
    "werkkzeug4": "werkkzeug4.declarations.v1.json",
}
INVENTORIES = {
    "werkkzeug3": "werkkzeug3.inventory.v1.json",
    "werkkzeug4": "werkkzeug4.inventory.v1.json",
}
COVERAGE = "semantic-coverage.v1.json"
CANDIDATES = "implementation-candidates.v1.json"


class CatalogError(ValueError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def declaration_identity(entry: Mapping[str, Any]) -> str:
    payload = {
        key: entry.get(key)
        for key in (
            "engine",
            "variant",
            "declaration_identity",
            "kind",
            "types",
            "ports",
            "extractable_parameters",
            "source_location",
            "hidden",
            "obsolete",
            "extraction_limits",
        )
    }
    return sha256_text(canonical_json(payload))


def regenerate_inventory(source_document: Mapping[str, Any], declarations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    engine = source_document["engine"]
    scopes = source_document["scope"]
    entries = []
    seen: set[str] = set()
    for declaration in declarations:
        identifier = declaration.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise CatalogError("declaration id must be a non-empty string")
        if identifier in seen:
            raise CatalogError(f"duplicate declaration id '{identifier}'")
        seen.add(identifier)
        variant = declaration.get("variant", engine)
        entry_scope = scopes.get(variant, scopes.get(engine))
        if entry_scope is None:
            raise CatalogError(f"declaration '{identifier}' uses unknown variant '{variant}'")
        entries.append(
            {
                "id": identifier,
                "engine": engine,
                "variant": variant,
                "engine_scope": entry_scope,
                "declaration_identity": declaration.get("declaration_identity"),
                "declaration_hash": declaration_identity(declaration),
                "kind": declaration.get("kind"),
                "types": declaration.get("types", []),
                "ports": declaration.get("ports", {"inputs": [], "outputs": []}),
                "extractable_parameters": declaration.get("extractable_parameters", []),
                "source_location": declaration.get("source_location"),
                "hidden": bool(declaration.get("hidden", False)),
                "obsolete": bool(declaration.get("obsolete", False)),
                "extraction_limits": declaration.get("extraction_limits", []),
                "review_state": declaration.get("review_state", "unreviewed"),
                "license": declaration.get("license", "upstream_unknown"),
            }
        )
    entries.sort(key=lambda item: (item["variant"], item["id"]))
    inventory = {
        "format": "nw13-operator-inventory",
        "version": 1,
        "engine": engine,
        "pinned_source": source_document["pinned_source"],
        "source_commit": source_document["source_commit"],
        "inventory_complete_against": "pinned-declaration-export",
        "entry_count": len(entries),
        "variants": sorted({entry["variant"] for entry in entries}),
        "entries": entries,
    }
    inventory["inventory_sha256"] = sha256_text(canonical_json(inventory))
    return inventory


def _verify_source_boundary(repo_root: Path) -> list[str]:
    errors: list[str] = []
    source_doc = read_json(repo_root / CATALOG_DIR / SOURCES)
    for source in source_doc.get("sources", []):
        if source.get("id") == "fr_public":
            if source.get("commit") != "8c8f82c2ae01133370e649bbc05916e7aceecadd":
                errors.append("fr_public pinned commit drifted from the recorded submodule commit")
            if source.get("vendored") is not False:
                errors.append("fr_public must remain unvendored")
    if not (repo_root / "references" / "farbrausch").exists():
        errors.append("references/farbrausch must remain the untouched submodule boundary")
    return errors


def _verified_review_fields(candidates: Sequence[Mapping[str, Any]]) -> list[str]:
    errors: list[str] = []
    required = {
        "production_need",
        "controls",
        "animation",
        "downstream",
        "temporal",
        "realization",
        "backend",
        "implementation_state",
        "decision",
    }
    allowed = {
        "preserve",
        "consolidate",
        "replace_implementation",
        "partial",
        "defer",
        "omit",
    }
    for candidate in candidates:
        missing = sorted(required - set(candidate))
        if missing:
            errors.append(f"candidate '{candidate.get('id')}' is missing review fields: {missing}")
        if candidate.get("decision") not in allowed:
            errors.append(f"candidate '{candidate.get('id')}' has invalid decision '{candidate.get('decision')}'")
    return errors


def verify_catalog(repo_root: Path) -> dict[str, Any]:
    catalog_dir = repo_root / CATALOG_DIR
    errors: list[str] = []
    errors.extend(_verify_source_boundary(repo_root))
    source_doc = read_json(catalog_dir / SOURCES)
    generated: dict[str, dict[str, Any]] = {}
    for engine, filename in DECLARATIONS.items():
        declarations_doc = read_json(catalog_dir / filename)
        if declarations_doc.get("engine") != engine:
            errors.append(f"{filename} engine is not '{engine}'")
        source = next(
            (entry for entry in source_doc.get("engines", []) if entry.get("id") == engine),
            None,
        )
        if source is None:
            errors.append(f"pinned source has no engine declaration for '{engine}'")
            continue
        generated[engine] = regenerate_inventory(source, declarations_doc.get("declarations", []))
        checked_in_path = catalog_dir / INVENTORIES[engine]
        if not checked_in_path.exists():
            errors.append(f"missing inventory {checked_in_path}")
            continue
        checked_in = read_json(checked_in_path)
        if canonical_json(checked_in) != canonical_json(generated[engine]):
            errors.append(f"{INVENTORIES[engine]} is not reproducible from the pinned declaration export")
        # Every declaration field must survive regeneration.
        generated_ids = {entry["id"] for entry in generated[engine]["entries"]}
        declaration_ids = {entry["id"] for entry in declarations_doc.get("declarations", [])}
        if generated_ids != declaration_ids:
            errors.append(f"{engine} inventory does not cover the complete declared id set")
        # kkrieger must remain identifiable and separately scoped.
        for entry in generated[engine]["entries"]:
            if entry["variant"] == "kkrieger":
                if entry["engine_scope"].get("identifiable") is not True:
                    errors.append(f"kkrieger entry '{entry['id']}' is not separately identifiable")
    coverage = read_json(catalog_dir / COVERAGE)
    candidates_doc = read_json(catalog_dir / CANDIDATES)
    candidate_ids = {entry["id"] for entry in candidates_doc.get("candidates", [])}
    catalog_entries = {
        entry["id"] for inventory in generated.values() for entry in inventory["entries"]
    }
    for link in coverage.get("links", []):
        if link.get("catalog_entry") not in catalog_entries:
            errors.append(f"coverage link references unknown catalog entry '{link.get('catalog_entry')}'")
        if link.get("candidate") not in candidate_ids:
            errors.append(f"coverage link references unknown implementation candidate '{link.get('candidate')}'")
    errors.extend(_verified_review_fields(candidates_doc.get("candidates", [])))
    gaps = candidates_doc.get("unresolved_gaps", [])
    for required_gap in ("mesh-topology", "selection", "uv"):
        if not any(gap.get("id") == required_gap for gap in gaps):
            errors.append(f"unresolved gap '{required_gap}' must remain recorded")
    return {
        "ok": not errors,
        "errors": errors,
        "engines": {engine: inventory["entry_count"] for engine, inventory in generated.items()},
        "variants": {engine: inventory["variants"] for engine, inventory in generated.items()},
        "coverage_links": len(coverage.get("links", [])),
        "candidates": len(candidates_doc.get("candidates", [])),
        "unreviewed_entries": sum(
            1 for inventory in generated.values() for entry in inventory["entries"] if entry["review_state"] == "unreviewed"
        ),
    }


def write_catalog(repo_root: Path) -> dict[str, Any]:
    catalog_dir = repo_root / CATALOG_DIR
    source_doc = read_json(catalog_dir / SOURCES)
    written: dict[str, int] = {}
    for engine, filename in DECLARATIONS.items():
        source = next(entry for entry in source_doc["engines"] if entry["id"] == engine)
        declarations = read_json(catalog_dir / filename)["declarations"]
        inventory = regenerate_inventory(source, declarations)
        write_json(catalog_dir / INVENTORIES[engine], inventory)
        written[engine] = inventory["entry_count"]
    return {"written": written}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--write", action="store_true", help="regenerate checked-in inventories")
    args = parser.parse_args(argv)
    try:
        result = write_catalog(args.repo_root) if args.write else verify_catalog(args.repo_root)
    except (CatalogError, OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)]}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())

