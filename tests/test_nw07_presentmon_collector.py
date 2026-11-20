from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw07_presentmon_collector as collector  # noqa: E402


def test_extract_samples_accepts_presentmon_v2_metric_names() -> None:
    rows = [
        {
            "ProcessID": "77",
            "GPUTime": "5.25",
            "CPUBusy": "1.75",
            "MsInPresentAPI": "0.25",
            "MsBetweenDisplayChange": "16.6667",
        },
        {
            "ProcessID": "77",
            "GPUTime": "5.50",
            "CPUBusy": "1.80",
            "MsInPresentAPI": "0.20",
            "MsBetweenDisplayChange": "NA",
        },
        {
            "ProcessID": "88",
            "GPUTime": "99",
            "CPUBusy": "99",
            "MsInPresentAPI": "1",
            "MsBetweenDisplayChange": "99",
        },
    ]
    gpu, cpu, displayed = collector.extract_samples(rows, process_id=77)
    assert gpu == [5.25, 5.5]
    assert cpu == [2.0, 2.0]
    assert displayed == [16.6667]


def test_extract_samples_accepts_legacy_ms_prefixed_metric_names() -> None:
    rows = [
        {
            "ProcessID": "100",
            "MsGPUTime": "4.0",
            "MsCPUBusy": "2.0",
            "MsInPresentAPI": "0.5",
            "DisplayedTime": "8.333",
        }
    ]
    gpu, cpu, displayed = collector.extract_samples(rows, process_id=100)
    assert gpu == [4.0]
    assert cpu == [2.5]
    assert displayed == [8.333]


def test_metric_value_rejects_negative_or_nonfinite_values() -> None:
    import pytest

    for value in ("-1", "inf", "-inf"):
        with pytest.raises(ValueError):
            collector._metric_value(value)
    assert collector._metric_value("NA") is None
    assert collector._metric_value("") is None


def test_write_samples_uses_real_line_breaks(tmp_path: Path) -> None:
    path = tmp_path / "samples.txt"
    collector._write_samples(path, [1.25, 2.5, 3.75])
    assert path.read_text(encoding="utf-8") == "1.250000000\n2.500000000\n3.750000000\n"
    assert [float(line) for line in path.read_text(encoding="utf-8").splitlines()] == [
        1.25,
        2.5,
        3.75,
    ]


def test_cpu_submit_requires_present_api_component() -> None:
    rows = [
        {
            "ProcessID": "7",
            "MsGPUTime": "3.0",
            "MsCPUBusy": "1.0",
            "MsBetweenDisplayChange": "16.0",
        }
    ]
    gpu, cpu, displayed = collector.extract_samples(rows, process_id=7)
    assert gpu == [3.0]
    assert cpu == []
    assert displayed == [16.0]
