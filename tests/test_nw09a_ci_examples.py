from __future__ import annotations

import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw09_ci_examples as ci_examples  # noqa: E402


def test_nw09a_examples_are_the_canonical_nw09_fixtures() -> None:
    rows = {
        name: (graph.relative_to(REPO_ROOT).as_posix(), filename)
        for name, graph, filename in ci_examples.EXAMPLES
    }
    assert rows == {
        "particles": (
            "fixtures/nw09/particles_seeded.nwgraph",
            "nw09-particles.exe",
        ),
        "feedback": (
            "fixtures/nw09/feedback_rect.nwgraph",
            "nw09-feedback.exe",
        ),
    }
    assert ci_examples.ASTEROID_TUNNEL_NAME == "asteroid-tunnel"
    assert ci_examples.ASTEROID_TUNNEL_FILENAME == "asteroid-tunnel.exe"


def test_nw09a_smoke_markers_require_stateful_progress() -> None:
    ci_examples._validate_smoke_stdout(
        "\n".join(
            [
                "NW05_LINK_MANIFEST_VERIFIED=1",
                "NW05_EXPLICIT_TICK=3200",
                "NW05_HISTORY_SIMULATION_TICK=8",
                "NW05_HISTORY_PENDING_TICKS=0",
            ]
        ),
        example="fixture",
    )

    with pytest.raises(ci_examples.ExampleBuildError):
        ci_examples._validate_smoke_stdout(
            "\n".join(
                [
                    "NW05_LINK_MANIFEST_VERIFIED=1",
                    "NW05_EXPLICIT_TICK=3200",
                    "NW05_HISTORY_SIMULATION_TICK=7",
                    "NW05_HISTORY_PENDING_TICKS=1",
                ]
            ),
            example="fixture",
        )




def test_asteroid_tunnel_smoke_does_not_claim_history_state() -> None:
    ci_examples._validate_smoke_stdout(
        "\n".join(
            [
                "NW05_LINK_MANIFEST_VERIFIED=1",
                "NW05_EXPLICIT_TICK=3200",
            ]
        ),
        example="asteroid-tunnel",
        require_history=False,
    )

    with pytest.raises(ci_examples.ExampleBuildError):
        ci_examples._validate_smoke_stdout(
            "NW05_LINK_MANIFEST_VERIFIED=1",
            example="asteroid-tunnel",
            require_history=False,
        )

def test_nw09a_sha256_manifest_covers_staged_executables(tmp_path: Path) -> None:
    particles = tmp_path / "particles" / "nw09-particles.exe"
    feedback = tmp_path / "feedback" / "nw09-feedback.exe"
    tunnel = tmp_path / "asteroid-tunnel" / "asteroid-tunnel.exe"
    particles.parent.mkdir()
    feedback.parent.mkdir()
    tunnel.parent.mkdir()
    particles.write_bytes(b"MZ-particles")
    feedback.write_bytes(b"MZ-feedback")
    tunnel.write_bytes(b"MZ-asteroid-tunnel")

    manifest = ci_examples._write_sha256_manifest(
        tmp_path, [particles, feedback, tunnel]
    )
    ci_examples._verify_sha256_manifest(tmp_path, manifest)

    rows = manifest.read_text(encoding="ascii").splitlines()
    assert len(rows) == 3
    assert any(row.endswith("  particles/nw09-particles.exe") for row in rows)
    assert any(row.endswith("  feedback/nw09-feedback.exe") for row in rows)
    assert any(row.endswith("  asteroid-tunnel/asteroid-tunnel.exe") for row in rows)

    feedback.write_bytes(b"changed")
    with pytest.raises(ci_examples.ExampleBuildError):
        ci_examples._verify_sha256_manifest(tmp_path, manifest)
