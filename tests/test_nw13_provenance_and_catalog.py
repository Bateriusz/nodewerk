from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import operator_catalog  # noqa: E402


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False)


def test_actual_base_sha_and_merged_nw11_nw12_ancestry() -> None:
    base = json.loads((REPO_ROOT / "benchmarks" / "nw13" / "base.v1.json").read_text(encoding="utf-8"))
    head = _git("rev-parse", "HEAD").stdout.strip()
    assert base["implementation_base"] == "1c9e1559c8d43530c7b213461c6719b6aa43ae35"
    recorded = _git("merge-base", "--is-ancestor", base["implementation_base"], head)
    assert recorded.returncode == 0
    for commit in ("8ade0b7", "1c9e1559c8d43530c7b213461c6719b6aa43ae35"):
        ancestor = _git("merge-base", "--is-ancestor", commit, head)
        assert ancestor.returncode == 0, f"{commit} is not an ancestor of the recorded base"
    planning = base["planning_inputs"]
    assert planning["pr_48"]["is_implementation_base"] is False
    assert planning["catalog_pr_head_is_implementation_base"] is False
    assert base["nw11_versioned_decisions"]["mesh_shader"]["source"].endswith("mesh-shader.v1.json")
    assert base["nw11_versioned_decisions"]["ray_query"]["source"].endswith("ray-query.v1.json")


def test_catalog_regeneration_reproduces_complete_declared_inventory() -> None:
    result = operator_catalog.verify_catalog(REPO_ROOT)
    assert result["ok"] is True, result["errors"]
    assert result["engines"] == {"werkkzeug3": 499, "werkkzeug4": 370}
    assert result["unreviewed_entries"] > 0
    assert result["coverage_links"] >= 30


def test_kkrieger_variant_is_identifiable_and_not_merged_into_werkkzeug4() -> None:
    sources = json.loads((REPO_ROOT / "references" / "operator_catalog" / "pinned-sources.v1.json").read_text(encoding="utf-8"))
    fr = next(source for source in sources["sources"] if source["id"] == "fr_public")
    assert fr["commit"] == "8c8f82c2ae01133370e649bbc05916e7aceecadd"
    assert fr["vendored"] is False
    wk3 = json.loads((REPO_ROOT / "references/operator_catalog/werkkzeug3.json").read_text())
    wk4 = json.loads((REPO_ROOT / "references/operator_catalog/werkkzeug4.json").read_text())
    variants = {entry["variant"] for entry in wk3["operators"]}
    assert variants == {"werkkzeug3", "werkkzeug3_kkrieger"}
    assert all(entry["key"].startswith(entry["variant"] + ":") for entry in wk3["operators"])
    assert all(entry["key"].startswith("wz4:") for entry in wk4["operators"])
    assert {source["id"] for source in sources["other_engines"]} == {"theprodukkt"}


def test_inventory_preserves_hidden_obsolete_types_ports_parameters_locations_and_limits() -> None:
    directory = REPO_ROOT / "references" / "operator_catalog"
    wk3 = json.loads((directory / "werkkzeug3.json").read_text())
    wk4 = json.loads((directory / "werkkzeug4.json").read_text())
    assert len(wk4["source_files"]) == 33
    assert {"hidden", "obsolete"} <= {role for e in wk4["operators"] for role in e["source_roles"]}
    for catalog in (wk3, wk4):
        assert catalog["extraction_notes"]
        for entry in catalog["operators"]:
            assert entry["parameters"]
            assert entry["source"]["line"] > 0
            assert catalog["source_revision"] in entry["source"]["url"]
            assert entry["key"]


def test_candidates_retain_review_decisions_and_unresolved_gaps() -> None:
    candidates = json.loads((REPO_ROOT / "references" / "operator_catalog" / "implementation-candidates.v1.json").read_text(encoding="utf-8"))
    decisions = {entry["decision"] for entry in candidates["candidates"]}
    assert decisions <= {"preserve", "consolidate", "replace_implementation", "partial", "defer", "omit"}
    assert {"preserve", "consolidate", "partial", "omit"} <= decisions
    gap_ids = {gap["id"] for gap in candidates["unresolved_gaps"]}
    assert {"mesh-topology", "selection", "uv"} <= gap_ids
    for entry in candidates["candidates"]:
        assert entry["production_need"]
        assert entry["realization"]
        assert entry["backend"]
        assert entry["implementation_state"]


def test_reference_boundary_is_preserved() -> None:
    proc = _git("status", "--porcelain", "references/farbrausch")
    assert proc.stdout.strip() == ""
    assert (REPO_ROOT / "references" / "farbrausch").exists()

