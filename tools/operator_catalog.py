#!/usr/bin/env python3
"""Verify the pinned source inventory and the NW-13 review links."""
from __future__ import annotations

import json
from pathlib import Path

from reference_operator_catalog import CATALOG_DIR, EXTRACTORS, validate, main


def verify_catalog(repo_root: Path) -> dict:
    directory = repo_root / CATALOG_DIR.relative_to(CATALOG_DIR.parents[1])
    catalogs = {name: json.loads((directory / name).read_text(encoding="utf-8")) for name in EXTRACTORS}
    capabilities = json.loads((directory / "capability_map.json").read_text(encoding="utf-8"))
    technologies = json.loads((directory / "technology_candidates.json").read_text(encoding="utf-8"))
    validate(catalogs, capabilities, technologies)
    inventory = {entry["key"] for catalog in catalogs.values() for entry in catalog["operators"]}
    decisions = json.loads((directory / "implementation-candidates.v1.json").read_text(encoding="utf-8"))
    candidates = {entry["id"] for entry in decisions["candidates"]}
    links = json.loads((directory / "semantic-coverage.v1.json").read_text(encoding="utf-8"))["links"]
    reviewed = set()
    for link in links:
        key = link["catalog_entry"], link["candidate"]
        if key[0] not in inventory or key[1] not in candidates or key in reviewed:
            raise ValueError(f"invalid or duplicate production review link: {key}")
        reviewed.add(key)
    return {
        "ok": True,
        "errors": [],
        "engines": {Path(name).stem: len(entries["operators"]) for name, entries in catalogs.items()},
        "coverage_links": len(links),
        "unreviewed_entries": len(inventory - {entry for entry, _ in reviewed}),
    }


if __name__ == "__main__":
    try:
        main()
        verify_catalog(CATALOG_DIR.parents[1])
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(f"operator catalog: {error}") from error
