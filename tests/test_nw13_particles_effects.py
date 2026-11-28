from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw13_particles as particles  # noqa: E402
from schema_validator import validate_instance  # noqa: E402


EMITTER = {
    "id": "dust",
    "seed": 1234,
    "count": 24,
    "origin": [0.0, 1.0, 0.0],
    "direction": [0.0, 1.0, 0.0],
    "speed_min": 0.1,
    "speed_max": 0.4,
    "spread_degrees": 20.0,
    "gravity": [0.0, -1.0, 0.0],
    "drag": 0.05,
    "lifetime_ticks": 2400,
    "size_start": 0.08,
    "size_end": 0.01,
    "material": "dust",
    "depth_bias": 0.0,
}


def _document(**overrides: object) -> dict:
    document = {
        "format": "nw13.particles",
        "version": 1,
        "tick_rate_hz": 48000,
        "simulation_hz": 120,
        "emitter": dict(EMITTER),
        "history": {
            "reset_policy": "on-seek",
            "step_rate_hz": 120,
            "max_steps": 240,
            "max_memory_bytes": 1048576,
        },
    }
    document.update(overrides)
    return document


def test_schema_and_emitter_validation() -> None:
    schema = json.loads((REPO_ROOT / "schemas" / "nw13-particles.v1.json").read_text(encoding="utf-8"))
    validate_instance(_document(), schema, schema)
    emitter = particles.compile_emitter(EMITTER)
    assert emitter.count == 24
    assert emitter.lifetime_steps == 6
    with pytest.raises(particles.ParticleError):
        particles.compile_emitter({**EMITTER, "speed_min": 2.0, "speed_max": 1.0})


def test_seeded_particles_are_deterministic_and_seed_sensitive() -> None:
    first = particles.ParticleHistory(particles.compile_emitter(EMITTER))
    second = particles.ParticleHistory(particles.compile_emitter(EMITTER))
    assert first.advance_to(12) == second.advance_to(12)
    other = particles.ParticleHistory(particles.compile_emitter({**EMITTER, "seed": 1235}))
    assert first.advance_to(12) != other.advance_to(12)
    state = first.advance_to(12)
    assert all(particle.lifetime_steps == 6 for particle in state)
    assert all(particle.size >= 0.0 for particle in state)


def test_history_reconstructs_across_reset_seek_and_rewind() -> None:
    emitter = particles.compile_emitter(EMITTER)
    history = particles.ParticleHistory(emitter, max_steps=240)
    fresh = particles.ParticleHistory(emitter, max_steps=240)
    expected = fresh.advance_to(18)
    history.advance_to(30)
    assert history.advance_to(18) == expected
    history.advance_to(50)
    assert history.rewind(32) == expected
    history.reset()
    assert history.advance_to(18) == expected


def test_history_seek_before_retained_window_reconstructs_from_origin() -> None:
    emitter = particles.compile_emitter(EMITTER)
    history = particles.ParticleHistory(emitter, max_steps=10)
    fresh = particles.ParticleHistory(emitter, max_steps=10)
    expected = fresh.advance_to(5)
    history.advance_to(40)
    assert min(history.checkpoints) > 5
    assert history.seek(5) == expected
    assert history.step == 5
    assert min(history.checkpoints) == 0
    assert history.rewind(10) == fresh.advance_to(0)


def test_history_memory_bound_is_explicit() -> None:
    emitter = particles.compile_emitter({**EMITTER, "count": 4000})
    with pytest.raises(particles.ParticleError) as exc:
        particles.ParticleHistory(emitter, max_steps=240, max_memory_bytes=1024)
    assert any(item["code"] == "particles_memory_budget_exceeded" for item in exc.value.diagnostics)


def test_depth_occlusion_uses_scene_depth() -> None:
    camera = particles.CameraDepth(
        position=(0.0, 0.0, 0.0),
        forward=(0.0, 0.0, -1.0),
        up=(0.0, 1.0, 0.0),
        fov_degrees=60.0,
        near=0.01,
        far=100.0,
        aspect=1.0,
        width=8,
        height=8,
    )
    near = particles.Particle((0.0, 0.0, -1.0), (0.0, 0.0, 0.0), 0, 10, 0.1)
    far = particles.Particle((0.0, 0.0, -5.0), (0.0, 0.0, 0.0), 0, 10, 0.1)
    mask = particles.occlusion_mask((near, far), camera, lambda x, y: 2.0)
    assert mask == [False, True]
    metadata = particles.depth_metadata(camera)
    assert metadata["depth_convention"] == "positive_view_space_linear"
    assert metadata["alpha_convention"] == "straight"


def test_feedback_consumes_current_and_prior_state_and_rejects_occluded_history() -> None:
    current = [1.0, 2.0, 3.0, 1.0]
    previous = [2.0, 2.0, 2.0, 1.0]
    assert particles.feedback(current, previous, decay=0.5, gain=1.0) == pytest.approx([2.0, 3.0, 4.0, 1.5])
    assert particles.feedback(current, previous, decay=0.5, gain=1.0, occluded=[False, True, False, False]) == pytest.approx([2.0, 2.0, 4.0, 1.5])
    assert particles.feedback(current, previous, decay=0.5, gain=1.0, discontinuity=True) == pytest.approx(current)
    assert particles.is_history_discontinuity([{"kind": "cut"}, {"kind": "cue"}]) is True
    assert particles.is_history_discontinuity([{"kind": "loop"}]) is True
    assert particles.is_history_discontinuity([{"kind": "seek"}]) is True
    assert particles.is_history_discontinuity([{"kind": "cue"}]) is False


def test_composite_bloom_and_tonemap_conventions() -> None:
    scene = [1.0, 0.0, 0.0, 1.0] * 4
    effect = [0.0, 1.0, 0.0, 0.5] * 4
    straight = particles.composite(scene, effect, width=2, height=2)
    assert straight[:4] == pytest.approx([0.5, 0.5, 0.0, 1.0])
    premultiplied = particles.composite(scene, [0.0, 0.5, 0.0, 0.5] * 4, width=2, height=2, alpha_convention="premultiplied")
    assert premultiplied[:4] == pytest.approx([0.5, 0.5, 0.0, 1.0])
    bright = [4.0, 4.0, 4.0, 1.0] * 4
    bloomed = particles.bloom(bright, 2, 2, threshold=0.5, intensity=0.25, radius=1)
    assert bloomed[0] > bright[0]
    toned = particles.tonemap(bloomed, exposure=1.0, gamma=2.2, operator="reinhard")
    assert all(0.0 <= toned[index] <= 1.0 for index in range(0, len(toned), 4))

