"""The native common comparison must compare bytes, not only size."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import nw11_common_regression as regression


def test_nw11_common_binary_difference_is_detected():
    before = {"executable_bytes": 1024, "executable_sha256": "a" * 64,
              "pe_imports": ["d3d12.dll"]}
    after = dict(before)
    assert regression.differences(before, after) == []
    after["executable_sha256"] = "b" * 64
    assert regression.differences(before, after) == ["executable_sha256"]
    after["pe_imports"] = ["d3d12.dll", "dxcompiler.dll"]
    assert regression.differences(before, after) == ["executable_sha256", "pe_imports"]
