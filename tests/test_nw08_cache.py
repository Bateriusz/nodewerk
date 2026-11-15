from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import nw08_cache as cache  # noqa: E402


SEMANTIC = "sha256:v1:" + "a" * 64
OTHER_SEMANTIC = "sha256:v1:" + "b" * 64


def _inputs(**overrides) -> cache.CacheKeyInputs:
    values = {
        "semantic_digest": SEMANTIC,
        "toolchain": cache.ToolchainIdentity(),
        "compiler_settings": {"optimization": "O2", "feature_level": "12_0"},
    }
    values.update(overrides)
    return cache.CacheKeyInputs(**values)


def _generation(**overrides) -> cache.DeviceGeneration:
    values = {
        "epoch": 0,
        "adapter": cache.AdapterDeviceIdentity(
            vendor_id=0x10DE, device_id=0x2805, subsystem_id=1, adapter_luid="luid-a"
        ),
        "capability_profile": cache.CapabilityProfileIdentity(),
        "driver_version": "566.36",
        "os_build": "10.0.26100.2314",
        "toolchain_identity": "msvc-ltcg",
    }
    values.update(overrides)
    return cache.DeviceGeneration(**values)


def test_compiler_keys_contain_every_semantic_compiler_target_input() -> None:
    base = _inputs()
    base_key = base.compiler_key().key
    assert base_key != _inputs(semantic_digest=OTHER_SEMANTIC).compiler_key().key
    assert (
        base_key
        != _inputs(compiler_settings={"optimization": "O1"}).compiler_key().key
    )
    assert (
        base_key
        != _inputs(compiler_inputs={"dxc": "1.8.2407"}).compiler_key().key
    )
    assert (
        base_key
        != _inputs(semantic_inputs={"seed": 7}).compiler_key().key
    )
    changed_toolchain = cache.ToolchainIdentity(target="d3d12.fl12_0.sm6_0.alt")
    assert base_key != _inputs(toolchain=changed_toolchain).compiler_key().key


def test_capability_profile_changes_invalidate_compiler_and_device_targets() -> None:
    first = _inputs()
    second = _inputs(
        capability_profile=cache.CapabilityProfileIdentity(
            id="nw.other.d3d12.fl12_0.sm6_0.v1",
            requirements_digest="sha256:v1:" + "c" * 64,
        )
    )
    assert first.compiler_key().key != second.compiler_key().key
    assert (
        first.device_realization_key(_generation()).key
        != second.device_realization_key(_generation()).key
    )


def test_marketing_names_never_affect_keys_but_adapter_identity_does() -> None:
    observed_a = {
        "gpu_model": "NVIDIA GeForce RTX 4060 Ti",
        "gpu_generation": "Ada",
        "adapter_description": "RTX 4060 Ti",
        "pci_vendor_id": 0x10DE,
        "pci_device_id": 0x2805,
        "pci_subsystem_id": 1,
        "adapter_luid": "luid-a",
    }
    observed_b = dict(observed_a)
    observed_b.update(
        {
            "gpu_model": "NVIDIA GeForce RTX 5070 Ti",
            "gpu_generation": "Blackwell",
            "adapter_description": "RTX 5070 Ti",
        }
    )
    inputs_a = _inputs(observed_hardware=observed_a)
    inputs_b = _inputs(observed_hardware=observed_b)
    assert inputs_a.compiler_key().key == inputs_b.compiler_key().key
    assert (
        inputs_a.device_realization_key(_generation()).key
        == inputs_b.device_realization_key(_generation()).key
    )

    adapter_a = cache.AdapterDeviceIdentity.from_observed_hardware(observed_a)
    adapter_b = cache.AdapterDeviceIdentity.from_observed_hardware(observed_b)
    assert adapter_a == adapter_b

    different_adapter = cache.AdapterDeviceIdentity.from_observed_hardware(
        {**observed_a, "pci_device_id": 0x2684, "adapter_luid": "luid-b"}
    )
    assert (
        inputs_a.compiler_key().key
        == inputs_b.compiler_key().key
    )
    assert (
        inputs_a.device_realization_key(_generation(adapter=different_adapter)).key
        != inputs_a.device_realization_key(_generation()).key
    )


def test_device_generation_includes_driver_os_toolchain_and_epoch() -> None:
    inputs = _inputs()
    baseline = inputs.device_realization_key(_generation()).key
    assert (
        inputs.device_realization_key(_generation(epoch=1)).key != baseline
    )
    assert (
        inputs.device_realization_key(_generation(driver_version="999.99")).key
        != baseline
    )
    assert (
        inputs.device_realization_key(_generation(os_build="10.0.99999.0")).key
        != baseline
    )
    assert (
        inputs.device_realization_key(
            _generation(toolchain_identity="clang-cl-lld")
        ).key
        != baseline
    )
    # Actual adapter identity appears only in device-resident keys.
    other_adapter = cache.AdapterDeviceIdentity(0x1002, 0x744C, 2, "luid-other")
    assert (
        inputs.compiler_key().key
        == _inputs().compiler_key().key
    )
    assert (
        inputs.device_realization_key(_generation(adapter=other_adapter)).key
        != baseline
    )


def test_device_epoch_increments_only_on_explicit_reinitialization() -> None:
    manager = cache.DeviceEpochManager(
        cache.AdapterDeviceIdentity(0x10DE, 0x2805, 0, "luid")
    )
    assert manager.epoch == 0
    assert manager.handle_device_loss().lost is True
    assert manager.epoch == 0
    generation = manager.reinitialize()
    assert manager.lost is False
    assert generation.epoch == 1
    # An explicit device-instance recreation always increments the epoch.
    assert manager.reinitialize().epoch == 2


def test_gpu_cache_is_bounded_including_retirement_and_fence_safe() -> None:
    gpu = cache.GpuRealizationCache(1000)
    first = gpu.acquire("a", 600, epoch=0)
    gpu.submit(["a"], 10)
    second = gpu.acquire("b", 200, epoch=0)
    gpu.submit(["b"], 20)
    assert first.key == "a"
    assert second.key == "b"
    assert gpu.total_bytes == 800

    # All retained bytes are referenced by submitted work.  The cache must not
    # destroy them to satisfy a new admission.
    with pytest.raises(cache.GpuBudgetExceeded):
        gpu.acquire("c", 300, epoch=0)
    assert gpu.total_bytes <= gpu.budget_bytes
    assert gpu.lookup("a", epoch=0) is not None
    assert gpu.lookup("b", epoch=0) is not None

    # Even an explicit release cannot destroy submitted work early.  The
    # resource remains accounted for in the retirement set.
    assert gpu.release("a") is True
    assert gpu.retiring_bytes == 600
    assert "a" in gpu.retiring_keys()
    assert gpu.signal_fence(9) == ()
    assert gpu.retiring_bytes == 600
    retired = gpu.signal_fence(10)
    assert retired == ("a",)
    assert gpu.retiring_bytes == 0
    third = gpu.acquire("c", 200, epoch=0)
    assert third.key == "c"
    assert gpu.total_bytes == 400
    assert gpu.within_budget is True


def test_gpu_cache_rejects_admission_above_budget() -> None:
    gpu = cache.GpuRealizationCache(100)
    with pytest.raises(cache.GpuBudgetExceeded):
        gpu.acquire("too-large", 101, epoch=0)
    gpu.acquire("a", 100, epoch=0)
    gpu.submit(["a"], 1)
    with pytest.raises(cache.GpuBudgetExceeded):
        gpu.acquire("b", 1, epoch=0)
    assert gpu.total_bytes <= gpu.budget_bytes


def test_async_queue_rejects_out_of_order_cancel_obsolete_and_lost() -> None:
    queue = cache.AsyncJobQueue(SEMANTIC)
    first = queue.submit()
    second = queue.submit()
    assert queue.complete(second).accepted is True
    stale = queue.complete(first)
    assert stale.accepted is False
    assert stale.reason == "superseded_by_newer_job"

    third = queue.submit()
    queue.cancel(third.job_id)
    assert queue.complete(third).reason == "canceled"

    fourth = queue.submit()
    queue.note_snapshot(OTHER_SEMANTIC)
    assert queue.complete(fourth).reason == "obsolete_snapshot_generation"

    fifth = queue.submit()
    queue.handle_device_loss()
    assert queue.complete(fifth).reason == "device_lost"
    queue.reinitialize()
    assert queue.complete(fifth).reason == "obsolete_device_epoch"


def test_gpu_cache_invalidates_old_device_epoch_without_reuse() -> None:
    gpu = cache.GpuRealizationCache(1000)
    gpu.acquire("old", 100, epoch=0)
    gpu.submit(["old"], 3)
    assert gpu.invalidate_device(0) == 1
    assert gpu.lookup("old", epoch=0) is None
    with pytest.raises(cache.DeviceLostError):
        gpu.acquire("old", 100, epoch=0)
    gpu.acquire("new", 100, epoch=1)
    assert gpu.lookup("new", epoch=1) is not None
    assert "old" in gpu.retiring_keys()
    gpu.signal_fence(3)
    assert "old" not in gpu.retiring_keys()


def test_host_identity_is_observed_or_explicitly_unavailable() -> None:
    identity = cache.discover_host_device_identity()
    if identity.available:
        assert identity.adapter.available
        assert identity.adapter.vendor_id or identity.adapter.device_id
        assert identity.driver_version
    else:
        assert identity.adapter == cache.AdapterDeviceIdentity.unavailable()
        assert identity.adapter.vendor_id == 0
        assert identity.adapter.device_id == 0
        assert identity.adapter.adapter_luid == ""
        assert identity.detail
    # The unavailable sentinel must never masquerade as a real NVIDIA adapter.
    if os.name != "nt":
        assert identity.adapter.vendor_id != 0x10DE

