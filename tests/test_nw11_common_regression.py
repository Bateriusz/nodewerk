"""The native common comparison must compare bytes, not only size."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import canonical_export_reproducibility as regression
import nw11_production_binding as binding
import pytest


def test_nw11_common_binary_difference_is_detected():
    before = {"executable_bytes": 1024, "executable_sha256": "a" * 64,
              "pe_imports": ["d3d12.dll"]}
    after = dict(before)
    assert regression.differences(before, after) == []
    after["executable_sha256"] = "b" * 64
    assert regression.differences(before, after) == ["executable_sha256"]
    after["pe_imports"] = ["d3d12.dll", "dxcompiler.dll"]
    assert regression.differences(before, after) == ["executable_sha256", "pe_imports"]



def test_same_revision_reproducibility_is_bound_to_commit():
    commit = "a" * 40
    identity = {"package_sha256": "b" * 64, "manifest_sha256": "c" * 64}
    proof = {"format": "canonical-export-reproducibility.v2", "base_commit": commit,
             "candidate_commit": commit, "status": "verified", "differences": [],
             "base": identity, "candidate": dict(identity)}
    assert binding.verify_proof(proof, commit) == ("b" * 64, "c" * 64)
    with pytest.raises(ValueError):
        binding.verify_proof(proof, "d" * 40)
    proof["candidate"]["package_sha256"] = "e" * 64
    with pytest.raises(ValueError):
        binding.verify_proof(proof, commit)
