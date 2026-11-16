from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw03_compiler as nw03  # noqa: E402
import nw08_editor as editor  # noqa: E402
import nw09_temporal as temporal  # noqa: E402


PARTICLES = REPO_ROOT / "fixtures" / "nw09" / "particles_seeded.nwgraph"
FEEDBACK = REPO_ROOT / "fixtures" / "nw09" / "feedback_rect.nwgraph"


@pytest.fixture(autouse=True)
def _test_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODEWERK_NW03_TEST_DIGEST", "1")


def test_history_declarations_are_semantic_schema_content() -> None:
    document = nw03.load_graph(PARTICLES)
    node = document.nodes["particles"]
    assert node.frequency == "history"
    assert node.history is not None
    assert node.history.state_type == "nw.particles.v1"
    assert node.history.reset_policy == "on-loop"
    assert node.history.step_rate_hz == 120
    assert node.history.initial_value
    specs = temporal.history_specs(document)
    assert len(specs) == 1
    assert specs[0].state_bytes > 0
    assert specs[0].state_layout_hash.startswith("sha256:v1:")


def test_history_free_fixture_is_unchanged_by_temporal_vocabulary() -> None:
    document = nw03.load_graph(REPO_ROOT / "fixtures" / "nw05" / "procedural_image.nwgraph")
    assert not temporal.document_has_history(document)
    for node in document.nodes.values():
        assert node.frequency != "history"
        assert node.history is None


@pytest.mark.parametrize(
    "text,code",
    [
        (
            "nwgraph 1\n"
            "profile nw.common.d3d12.fl12_0.sm6_0.v1 "
            "sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a\n"
            "node bad nw.math.scalar_const@1.0.0 frequency=history\n"
            "  param value 1.0\n",
            "history_not_supported",
        ),
        (
            "nwgraph 1\n"
            "profile nw.common.d3d12.fl12_0.sm6_0.v1 "
            "sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a\n"
            "node bad nw.particles.system@1.0.0 frequency=history\n"
            "  history state-type nw.particles.v1 initial \"x\" reset on-loop step-rate 60\n",
            "invalid_history_step_rate",
        ),
        (
            "nwgraph 1\n"
            "profile nw.common.d3d12.fl12_0.sm6_0.v1 "
            "sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a\n"
            "node bad nw.math.scalar_const@1.0.0 frequency=frame\n"
            "  param value 1.0\n"
            "  history state-type nw.particles.v1 initial \"x\" reset on-loop step-rate 120\n",
            "invalid_history_declaration",
        ),
        (
            "nwgraph 1\n"
            "profile nw.common.d3d12.fl12_0.sm6_0.v1 "
            "sha256:v1:d2c53aeaebb76024b3ec89f59814a00e84aaa5131e190a802ede0a956302e17a\n"
            "node bad nw.particles.system@1.0.0 frequency=history\n"
            "  history state-type nw.particles.v1 initial \"x\" reset on-loop step-rate 120\n"
            "  input previous bad.out\n",
            "ordinary_cycle",
        ),
    ],
)
def test_invalid_history_declarations_fail_closed(text: str, code: str) -> None:
    document, diagnostics = nw03.parse_graph_text(text)
    assert document is None or any(
        item.get("severity") == "error" and item["code"] == code
        for item in diagnostics
    )


def test_previous_tick_state_is_external_not_an_author_edge() -> None:
    document = nw03.load_graph(FEEDBACK)
    node = document.nodes["feedback"]
    assert "previous" not in node.inputs
    spec = temporal.history_specs(document)[0]
    assert spec.reset_policy == "on-loop"
    # The output is a render image consumed by the ordinary frame DAG.
    assert document.nodes["screen"].inputs == {"image": ("feedback", "out")}


def test_editor_emission_round_trips_history_declarations() -> None:
    session = editor.GraphEditorSession.open(PARTICLES)
    text = session.source_text
    assert "history state-type nw.particles.v1" in text
    parsed, diagnostics = nw03.parse_graph_text(text)
    assert parsed is not None
    assert not [item for item in diagnostics if item.get("severity") == "error"]
    declaration = parsed.nodes["particles"].history
    assert declaration == session.document.nodes["particles"].history

