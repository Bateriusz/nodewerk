#!/usr/bin/env python3
"""NW-08 authoring cache identity, device generations, and fence-safe GPU caching.

This module is deliberately authoring-side only.  It contains no D3D12/DXGI
types and is never linked into ``nodewerk-player``.  It implements the
identity rules and lifetime bookkeeping the interactive editor needs on top of
the immutable NW-02 graph snapshots and the existing NW-03/NW-05 compiler
targets:

* semantic/compiler cache keys contain every relevant compiler, target, and
  semantic input, but only capability-profile identity for hardware;
* device-resident realization keys additionally contain the D3D12 device
  instance/recreation epoch, driver/OS/toolchain identity, and the actual
  adapter/device identity;
* GPU marketing names and generations are metadata only and can never affect a
  key, a compiler target, or a device epoch;
* GPU memory is bounded by a configured budget.  A realization referenced by
  submitted work is moved to a retirement queue instead of being destroyed
  until the corresponding fence is signalled.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from nwp_package import COMMON_PROFILE_ID, COMMON_REQUIREMENTS_DIGEST


class CacheIdentityError(ValueError):
    """Raised when a cache key input is malformed or ambiguous."""


class GpuBudgetExceeded(RuntimeError):
    """Raised when a bounded GPU realization cannot be admitted."""


class DeviceLostError(RuntimeError):
    """Raised when an operation targets a lost D3D12 device instance."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_value(value: Any) -> str:
    return "sha256:v1:" + sha256_bytes(canonical_json_bytes(value))


def _require_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CacheIdentityError(f"{name} must be a non-empty string")
    return value


def _require_int(name: str, value: Any, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CacheIdentityError(f"{name} must be an integer")
    if value < minimum:
        raise CacheIdentityError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class CapabilityProfileIdentity:
    """The only hardware-adjacent input allowed in compiler/semantic keys."""

    id: str = COMMON_PROFILE_ID
    requirements_digest: str = COMMON_REQUIREMENTS_DIGEST

    def __post_init__(self) -> None:
        _require_text("capability profile id", self.id)
        _require_text("capability requirements digest", self.requirements_digest)

    def to_key_fields(self) -> dict[str, str]:
        return {
            "capability_profile_id": self.id,
            "capability_requirements_digest": self.requirements_digest,
        }


@dataclass(frozen=True)
class AdapterDeviceIdentity:
    """Actual adapter/device identity for device-resident realizations only.

    ``adapter_description``, ``gpu_model``, and ``gpu_generation`` are
    intentionally absent.  Marketing names and RTX generation labels are
    observed metadata, never cache predicates.
    """

    vendor_id: int
    device_id: int
    subsystem_id: int = 0
    adapter_luid: str = ""
    device_removed: bool = False

    def __post_init__(self) -> None:
        _require_int("adapter vendor_id", self.vendor_id)
        _require_int("adapter device_id", self.device_id)
        _require_int("adapter subsystem_id", self.subsystem_id)
        if self.vendor_id > 0xFFFF or self.device_id > 0xFFFF:
            raise CacheIdentityError("adapter PCI identifiers must fit uint16")
        if self.subsystem_id > 0xFFFFFFFF:
            raise CacheIdentityError("adapter subsystem_id must fit uint32")
        if not isinstance(self.adapter_luid, str):
            raise CacheIdentityError("adapter_luid must be a string")

    def to_key_fields(self) -> dict[str, Any]:
        return {
            "vendor_id": self.vendor_id,
            "device_id": self.device_id,
            "subsystem_id": self.subsystem_id,
            "adapter_luid": self.adapter_luid,
        }

    @property
    def pci_vendor_id(self) -> int:
        return self.vendor_id

    @property
    def pci_device_id(self) -> int:
        return self.device_id

    @classmethod
    def from_observed_hardware(
        cls, observed: Mapping[str, Any]
    ) -> "AdapterDeviceIdentity":
        """Extract only stable identity fields from observed hardware metadata.

        Any ``gpu_model``, ``gpu_generation``, ``adapter_description``, or other
        marketing label in ``observed`` is ignored by construction.
        """
        if not isinstance(observed, Mapping):
            raise CacheIdentityError("observed hardware metadata must be an object")
        try:
            vendor_id = int(observed.get("pci_vendor_id", 0))
            device_id = int(observed.get("pci_device_id", 0))
        except (TypeError, ValueError) as exc:
            raise CacheIdentityError("observed PCI identifiers are not numeric") from exc
        subsystem = observed.get("pci_subsystem_id", observed.get("subsystem_id", 0))
        try:
            subsystem_id = int(subsystem)
        except (TypeError, ValueError) as exc:
            raise CacheIdentityError("observed subsystem id is not numeric") from exc
        return cls(
            vendor_id=vendor_id,
            device_id=device_id,
            subsystem_id=subsystem_id,
            adapter_luid=str(observed.get("adapter_luid", "")),
            device_removed=bool(observed.get("device_removed", False)),
        )


@dataclass(frozen=True)
class ToolchainIdentity:
    """Compiler/toolchain identity shared by compiler and device keys."""

    compiler_id: str = "nodewerkc"
    compiler_version: str = "1"
    dxc_version: str = "1.8.2407"
    target: str = "d3d12.fl12_0.sm6_0"
    optimization: str = "O2"
    os_build: str = ""

    def __post_init__(self) -> None:
        for name, value in (
            ("compiler_id", self.compiler_id),
            ("compiler_version", self.compiler_version),
            ("dxc_version", self.dxc_version),
            ("target", self.target),
            ("optimization", self.optimization),
        ):
            _require_text(name, value)
        if not isinstance(self.os_build, str):
            raise CacheIdentityError("os_build must be a string")

    def to_key_fields(self) -> dict[str, str]:
        return {
            "compiler_id": self.compiler_id,
            "compiler_version": self.compiler_version,
            "dxc_version": self.dxc_version,
            "target": self.target,
            "optimization": self.optimization,
            "os_build": self.os_build,
        }


@dataclass(frozen=True)
class DeviceGeneration:
    """One D3D12 device-instance recreation epoch.

    The epoch is incremented only by explicit preview reinitialization after a
    device loss.  The key includes capability-profile identity, driver/OS/
    toolchain identity, and the actual adapter/device identity.  It never
    includes a marketing name or RTX generation label.
    """

    epoch: int
    adapter: AdapterDeviceIdentity
    capability_profile: CapabilityProfileIdentity = field(
        default_factory=CapabilityProfileIdentity
    )
    driver_version: str = ""
    os_build: str = ""
    toolchain_identity: str = ""
    lost: bool = False

    def __post_init__(self) -> None:
        _require_int("device epoch", self.epoch)
        if not isinstance(self.adapter, AdapterDeviceIdentity):
            raise CacheIdentityError("device generation requires adapter identity")
        if not isinstance(self.capability_profile, CapabilityProfileIdentity):
            raise CacheIdentityError(
                "device generation requires capability-profile identity"
            )
        for name, value in (
            ("driver_version", self.driver_version),
            ("os_build", self.os_build),
            ("toolchain_identity", self.toolchain_identity),
        ):
            if not isinstance(value, str):
                raise CacheIdentityError(f"{name} must be a string")

    def to_key_fields(self) -> dict[str, Any]:
        return {
            "device_instance_epoch": self.epoch,
            "adapter": self.adapter.to_key_fields(),
            **self.capability_profile.to_key_fields(),
            "driver_version": self.driver_version,
            "os_build": self.os_build,
            "toolchain_identity": self.toolchain_identity,
        }

    @property
    def digest(self) -> str:
        return sha256_value(
            {"domain": "nw08.device-generation.v1", **self.to_key_fields()}
        )

    @property
    def device_epoch(self) -> int:
        return self.epoch

    @property
    def device_instance_epoch(self) -> int:
        return self.epoch

    @property
    def adapter_identity(self) -> AdapterDeviceIdentity:
        return self.adapter

    @property
    def capability_profile_id(self) -> str:
        return self.capability_profile.id

    @property
    def capability_requirements_digest(self) -> str:
        return self.capability_profile.requirements_digest

    @classmethod
    def from_identities(
        cls,
        *,
        epoch: int,
        adapter: AdapterDeviceIdentity,
        capability_profile_id: str = COMMON_PROFILE_ID,
        capability_requirements_digest: str = COMMON_REQUIREMENTS_DIGEST,
        driver_version: str = "",
        os_build: str = "",
        toolchain_identity: str = "",
        lost: bool = False,
    ) -> "DeviceGeneration":
        return cls(
            epoch=epoch,
            adapter=adapter,
            capability_profile=CapabilityProfileIdentity(
                capability_profile_id, capability_requirements_digest
            ),
            driver_version=driver_version,
            os_build=os_build,
            toolchain_identity=toolchain_identity,
            lost=lost,
        )


class DeviceEpochManager:
    """Tracks device loss and explicit reinitialization epochs."""

    def __init__(
        self,
        adapter: AdapterDeviceIdentity,
        *,
        capability_profile: CapabilityProfileIdentity | None = None,
        driver_version: str = "",
        os_build: str = "",
        toolchain_identity: str = "",
        epoch: int = 0,
    ):
        self._capability_profile = capability_profile or CapabilityProfileIdentity()
        self._generation = DeviceGeneration(
            epoch=epoch,
            adapter=adapter,
            capability_profile=self._capability_profile,
            driver_version=driver_version,
            os_build=os_build,
            toolchain_identity=toolchain_identity,
        )

    @property
    def generation(self) -> DeviceGeneration:
        return self._generation

    @property
    def epoch(self) -> int:
        return self._generation.epoch

    @property
    def lost(self) -> bool:
        return self._generation.lost

    def handle_device_loss(self) -> DeviceGeneration:
        if self._generation.lost:
            return self._generation
        self._generation = replace(self._generation, lost=True)
        return self._generation

    def reinitialize(
        self,
        *,
        adapter: AdapterDeviceIdentity | None = None,
        capability_profile: CapabilityProfileIdentity | None = None,
        driver_version: str | None = None,
        os_build: str | None = None,
        toolchain_identity: str | None = None,
    ) -> DeviceGeneration:
        """Explicitly recreate the device instance and increment the epoch."""
        self._generation = DeviceGeneration(
            epoch=self._generation.epoch + 1,
            adapter=adapter or self._generation.adapter,
            capability_profile=capability_profile or self._capability_profile,
            driver_version=(
                self._generation.driver_version
                if driver_version is None
                else driver_version
            ),
            os_build=(
                self._generation.os_build if os_build is None else os_build
            ),
            toolchain_identity=(
                self._generation.toolchain_identity
                if toolchain_identity is None
                else toolchain_identity
            ),
            lost=False,
        )
        return self._generation


@dataclass(frozen=True)
class CacheKey:
    scope: str
    digest: str
    components: Mapping[str, Any]

    def __post_init__(self) -> None:
        _require_text("cache scope", self.scope)
        _require_text("cache digest", self.digest)
        if not isinstance(self.components, Mapping):
            raise CacheIdentityError("cache key components must be a mapping")

    @property
    def key(self) -> str:
        return self.digest

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "digest": self.digest,
            "components": dict(self.components),
        }


@dataclass(frozen=True)
class CacheKeyInputs:
    """Every semantic/compiler/target input that can affect a realization."""

    semantic_digest: str
    capability_profile: CapabilityProfileIdentity = field(
        default_factory=CapabilityProfileIdentity
    )
    target_profile_id: str = COMMON_PROFILE_ID
    target_requirements_digest: str = COMMON_REQUIREMENTS_DIGEST
    toolchain: ToolchainIdentity = field(default_factory=ToolchainIdentity)
    compiler_settings: Mapping[str, Any] = field(default_factory=dict)
    semantic_inputs: Mapping[str, Any] = field(default_factory=dict)
    compiler_inputs: Mapping[str, Any] = field(default_factory=dict)
    shader_identity: str = ""
    entry_point: str = ""
    # Retained for callers that carry observed marketing metadata.  It is
    # explicitly excluded from every key by `to_compiler_fields`.
    observed_hardware: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text("semantic digest", self.semantic_digest)
        if not self.semantic_digest.startswith("sha256:v1:"):
            raise CacheIdentityError("semantic digest must use sha256:v1 form")
        _require_text("target profile id", self.target_profile_id)
        _require_text(
            "target requirements digest", self.target_requirements_digest
        )
        if not isinstance(self.capability_profile, CapabilityProfileIdentity):
            raise CacheIdentityError(
                "capability_profile must be CapabilityProfileIdentity"
            )
        if not isinstance(self.toolchain, ToolchainIdentity):
            raise CacheIdentityError("toolchain must be ToolchainIdentity")
        for name, value in (
            ("compiler_settings", self.compiler_settings),
            ("semantic_inputs", self.semantic_inputs),
            ("compiler_inputs", self.compiler_inputs),
            ("observed_hardware", self.observed_hardware),
        ):
            if not isinstance(value, Mapping):
                raise CacheIdentityError(f"{name} must be a mapping")

    def to_compiler_fields(self) -> dict[str, Any]:
        """Hardware-model-independent compiler/semantic key fields."""
        return {
            "domain": "nw08.compiler-realization.v1",
            "semantic_digest": self.semantic_digest,
            **self.capability_profile.to_key_fields(),
            "target_profile_id": self.target_profile_id,
            "target_requirements_digest": self.target_requirements_digest,
            "toolchain": self.toolchain.to_key_fields(),
            "compiler_settings": dict(self.compiler_settings),
            "semantic_inputs": dict(self.semantic_inputs),
            "compiler_inputs": dict(self.compiler_inputs),
            "shader_identity": self.shader_identity,
            "entry_point": self.entry_point,
        }

    def compiler_key(self) -> CacheKey:
        fields = self.to_compiler_fields()
        return CacheKey(
            scope="compiler",
            digest=sha256_value(fields),
            components=fields,
        )

    def device_realization_key(self, generation: DeviceGeneration) -> CacheKey:
        if not isinstance(generation, DeviceGeneration):
            raise CacheIdentityError(
                "device realization key requires a DeviceGeneration"
            )
        compiler_fields = self.to_compiler_fields()
        fields = {
            "domain": "nw08.device-realization.v1",
            "compiler_key": sha256_value(compiler_fields),
            "device_generation": generation.to_key_fields(),
            "device_generation_digest": generation.digest,
        }
        return CacheKey(
            scope="device-resident",
            digest=sha256_value(fields),
            components=fields,
        )


# ---------------------------------------------------------------------------
# Fence-safe GPU realization cache
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GenerationToken:
    """Identity of one asynchronous compilation/preview job."""

    job_id: int
    snapshot_generation: int
    device_epoch: int
    semantic_digest: str

    def __post_init__(self) -> None:
        _require_int("job_id", self.job_id)
        _require_int("snapshot_generation", self.snapshot_generation)
        _require_int("device_epoch", self.device_epoch)
        _require_text("semantic_digest", self.semantic_digest)


@dataclass(frozen=True)
class CompletionDecision:
    accepted: bool
    reason: str
    token: GenerationToken

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "token": {
                "job_id": self.token.job_id,
                "snapshot_generation": self.token.snapshot_generation,
                "device_epoch": self.token.device_epoch,
                "semantic_digest": self.token.semantic_digest,
            },
        }


class AsyncJobQueue:
    """Deterministic generation-token gate for asynchronous editors.

    A completion is accepted only when its token still names the current
    immutable snapshot generation and the current D3D12 device epoch, it was
    not cancelled, it has not already completed, and no newer job for the same
    snapshot has superseded it.
    """

    def __init__(self, semantic_digest: str = ""):
        self._next_job_id = 1
        self._snapshot_generation = 0
        self._device_epoch = 0
        self._semantic_digest = semantic_digest
        self._device_lost = False
        self._latest_by_snapshot: dict[int, int] = {}
        self._pending: dict[int, GenerationToken] = {}
        self._tokens: dict[int, GenerationToken] = {}
        self._canceled: set[int] = set()
        self._completed: set[int] = set()
        self._rejections: dict[str, int] = {}

    @property
    def snapshot_generation(self) -> int:
        return self._snapshot_generation

    @property
    def device_epoch(self) -> int:
        return self._device_epoch

    @property
    def semantic_digest(self) -> str:
        return self._semantic_digest

    @property
    def device_lost(self) -> bool:
        return self._device_lost

    @property
    def pending_jobs(self) -> tuple[int, ...]:
        return tuple(sorted(self._pending))

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._rejections)

    def note_snapshot(self, semantic_digest: str | None = None) -> int:
        """Advance the immutable snapshot generation."""
        self._snapshot_generation += 1
        if semantic_digest is not None:
            _require_text("semantic_digest", semantic_digest)
            self._semantic_digest = semantic_digest
        return self._snapshot_generation

    def submit(self, *, semantic_digest: str | None = None) -> GenerationToken:
        if self._device_lost:
            raise DeviceLostError(
                "cannot submit preview work after device loss; reinitialize first"
            )
        if semantic_digest is not None:
            self._semantic_digest = semantic_digest
        token = GenerationToken(
            job_id=self._next_job_id,
            snapshot_generation=self._snapshot_generation,
            device_epoch=self._device_epoch,
            semantic_digest=self._semantic_digest,
        )
        self._next_job_id += 1
        self._pending[token.job_id] = token
        self._tokens[token.job_id] = token
        self._latest_by_snapshot[token.snapshot_generation] = token.job_id
        return token

    def cancel(self, job_id: int) -> bool:
        if job_id not in self._pending:
            return False
        self._canceled.add(job_id)
        self._pending.pop(job_id, None)
        return True

    def complete(self, token: GenerationToken | int) -> CompletionDecision:
        if isinstance(token, int):
            stored = self._tokens.get(token)
            token = stored or GenerationToken(
                token,
                self._snapshot_generation,
                self._device_epoch,
                self._semantic_digest,
            )
        if token.job_id in self._canceled:
            return self._reject(token, "canceled")
        if token.job_id in self._completed:
            return self._reject(token, "duplicate_completion")
        if self._device_lost:
            return self._reject(token, "device_lost")
        if token.device_epoch != self._device_epoch:
            return self._reject(token, "obsolete_device_epoch")
        if token.snapshot_generation != self._snapshot_generation:
            return self._reject(token, "obsolete_snapshot_generation")
        if self._latest_by_snapshot.get(token.snapshot_generation) != token.job_id:
            return self._reject(token, "superseded_by_newer_job")
        if token.job_id not in self._pending:
            return self._reject(token, "unknown_job")
        self._pending.pop(token.job_id, None)
        self._completed.add(token.job_id)
        return CompletionDecision(True, "accepted", token)

    def handle_device_loss(self) -> None:
        """Invalidate every pending job and refuse submissions until reinit."""
        self._device_lost = True
        self._pending.clear()
        self._latest_by_snapshot.clear()

    def reinitialize(self) -> int:
        """Explicitly recreate the device and increment its epoch."""
        self._device_epoch += 1
        self._device_lost = False
        return self._device_epoch

    def _reject(self, token: GenerationToken, reason: str) -> CompletionDecision:
        self._rejections[reason] = self._rejections.get(reason, 0) + 1
        return CompletionDecision(False, reason, token)


@dataclass
class GpuRealization:
    key: str
    size_bytes: int
    epoch: int
    last_fence: int | None = None
    last_used: int = 0
    state: str = "live"

    def __post_init__(self) -> None:
        _require_text("realization key", self.key)
        _require_int("realization size_bytes", self.size_bytes)
        _require_int("realization epoch", self.epoch)
        if self.last_fence is not None:
            _require_int("realization last_fence", self.last_fence)
        if self.state not in {"live", "retiring"}:
            raise CacheIdentityError("realization state must be live or retiring")


class GpuRealizationCache:
    """Bounded, fence-retiring cache for device-resident realizations.

    ``live_bytes + retiring_bytes`` never exceeds ``budget_bytes``.  A resource
    referenced by submitted GPU work is moved to the retiring set rather than
    destroyed when evicted; it is destroyed only when a fence value at least as
    large as its submission fence is signalled.
    """

    def __init__(self, budget_bytes: int):
        _require_int("GPU cache budget_bytes", budget_bytes, minimum=1)
        self.budget_bytes = budget_bytes
        self._live: dict[str, GpuRealization] = {}
        self._retiring: dict[str, GpuRealization] = {}
        self._completed_fence = 0
        self._max_submitted_fence = 0
        self._tick = 0
        self._lost_epochs: set[int] = set()
        self._stats = {
            "hits": 0,
            "misses": 0,
            "admissions": 0,
            "evictions": 0,
            "retirements": 0,
            "deferred_destructions": 0,
            "budget_rejections": 0,
            "stale_rejections": 0,
        }

    # -- identity / epoch -------------------------------------------------
    def set_budget(self, budget_bytes: int) -> None:
        _require_int("GPU cache budget_bytes", budget_bytes, minimum=1)
        if budget_bytes < self.live_bytes + self.retiring_bytes:
            raise GpuBudgetExceeded(
                "new GPU budget is below the currently retained realization bytes"
            )
        self.budget_bytes = budget_bytes

    def invalidate_device(self, epoch: int, *, force: bool = False) -> int:
        """Invalidate all realizations belonging to an old device epoch.

        Resources already submitted move to the retirement queue until their
        fence is signalled.  When ``force`` is true (device loss), the current
        submitted fence value is used, because the device can no longer make
        progress.  No old-epoch resource is ever returned to a new epoch.
        """
        _require_int("device epoch", epoch)
        self._lost_epochs.add(epoch)
        invalidated = 0
        entries = dict(self._live)
        entries.update(self._retiring)
        for key, entry in entries.items():
            if entry.epoch != epoch:
                continue
            invalidated += 1
            self._live.pop(key, None)
            if entry.last_fence is not None and not force:
                entry.state = "retiring"
                self._retiring[key] = entry
                self._stats["deferred_destructions"] += 1
            else:
                self._retiring.pop(key, None)
        return invalidated

    # -- accounting -------------------------------------------------------
    @property
    def live_bytes(self) -> int:
        return sum(entry.size_bytes for entry in self._live.values())

    @property
    def retiring_bytes(self) -> int:
        return sum(entry.size_bytes for entry in self._retiring.values())

    @property
    def pending_retirement_bytes(self) -> int:
        return self.retiring_bytes

    @property
    def total_bytes(self) -> int:
        return self.live_bytes + self.retiring_bytes

    @property
    def gpu_memory_budget_bytes(self) -> int:
        return self.budget_bytes

    @property
    def budget(self) -> int:
        return self.budget_bytes

    @property
    def within_budget(self) -> bool:
        return self.total_bytes <= self.budget_bytes

    @property
    def completed_fence(self) -> int:
        return self._completed_fence

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._live))

    def retiring_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._retiring))

    # -- cache operations -------------------------------------------------
    def lookup(self, key: str, *, epoch: int | None = None) -> GpuRealization | None:
        entry = self._live.get(key)
        if entry is None:
            self._stats["misses"] += 1
            return None
        if epoch is not None and entry.epoch != epoch:
            self._stats["stale_rejections"] += 1
            return None
        if entry.epoch in self._lost_epochs:
            self._stats["stale_rejections"] += 1
            return None
        self._tick += 1
        entry.last_used = self._tick
        self._stats["hits"] += 1
        return entry

    def acquire(
        self,
        key: str,
        size_bytes: int,
        *,
        epoch: int,
        fence: int | None = None,
    ) -> GpuRealization:
        _require_text("realization key", key)
        _require_int("realization size_bytes", size_bytes)
        _require_int("realization epoch", epoch)
        existing = self.lookup(key, epoch=epoch)
        if existing is not None:
            if fence is not None:
                existing.last_fence = max(existing.last_fence or 0, fence)
            return existing
        if size_bytes > self.budget_bytes:
            self._stats["budget_rejections"] += 1
            raise GpuBudgetExceeded(
                f"realization {key!r} needs {size_bytes} bytes, above the "
                f"{self.budget_bytes}-byte GPU cache budget"
            )
        if epoch in self._lost_epochs:
            self._stats["stale_rejections"] += 1
            raise DeviceLostError(
                f"refusing to admit realization for lost device epoch {epoch}"
            )
        self._make_room(size_bytes)
        self._tick += 1
        entry = GpuRealization(
            key=key,
            size_bytes=size_bytes,
            epoch=epoch,
            last_fence=fence,
            last_used=self._tick,
            state="live",
        )
        self._live[key] = entry
        self._stats["admissions"] += 1
        return entry

    def submit(self, keys: Iterable[str], fence: int) -> None:
        """Record that realized resources are referenced by submitted work."""
        _require_int("submission fence", fence)
        self._max_submitted_fence = max(self._max_submitted_fence, fence)
        for key in keys:
            entry = self._live.get(key)
            if entry is not None:
                entry.last_fence = max(entry.last_fence or 0, fence)

    def signal_fence(self, value: int) -> tuple[str, ...]:
        """Retire every resource whose last submission fence has completed."""
        _require_int("completed fence", value)
        self._completed_fence = max(self._completed_fence, value)
        retired: list[str] = []
        for key, entry in list(self._retiring.items()):
            if entry.last_fence is None or entry.last_fence <= self._completed_fence:
                self._retiring.pop(key, None)
                retired.append(key)
                self._stats["retirements"] += 1
        return tuple(sorted(retired))

    # Backwards-friendly aliases used by GUI/verifier code.
    retire_fence = signal_fence

    def release(self, key: str) -> bool:
        entry = self._live.get(key)
        if entry is None:
            return False
        if entry.last_fence is not None and entry.last_fence > self._completed_fence:
            self._live.pop(key, None)
            entry.state = "retiring"
            self._retiring[key] = entry
            self._stats["deferred_destructions"] += 1
        else:
            self._live.pop(key, None)
            self._stats["retirements"] += 1
        return True

    def clear(self) -> None:
        self._live.clear()
        self._retiring.clear()

    # -- internals --------------------------------------------------------
    def _make_room(self, needed: int) -> None:
        while self.total_bytes + needed > self.budget_bytes:
            candidate = self._oldest_evictable()
            if candidate is None:
                self._stats["budget_rejections"] += 1
                raise GpuBudgetExceeded(
                    "GPU cache budget is exhausted by work awaiting fence retirement"
                )
            self._evict(candidate.key)

    def _oldest_evictable(self) -> GpuRealization | None:
        candidates = [
            entry
            for entry in self._live.values()
            if entry.last_fence is None
            or entry.last_fence <= self._completed_fence
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda entry: entry.last_used)

    def _evict(self, key: str) -> None:
        entry = self._live.pop(key, None)
        if entry is None:
            return
        self._stats["evictions"] += 1
        if entry.last_fence is not None and entry.last_fence > self._completed_fence:
            entry.state = "retiring"
            self._retiring[key] = entry
            self._stats["deferred_destructions"] += 1
        else:
            self._stats["retirements"] += 1


# Aliases kept explicit so callers can use either descriptive spelling.
GpuCache = GpuRealizationCache
SemanticCompilerKey = CacheKeyInputs
SemanticCacheKey = CacheKey
DeviceCacheKey = CacheKey
DeviceEpoch = DeviceGeneration
PreviewJobQueue = AsyncJobQueue


def make_compiler_cache_key(inputs: CacheKeyInputs) -> str:
    """Convenience wrapper returning the compiler/semantic key digest."""
    return inputs.compiler_key().key


def make_device_realization_key(
    inputs: CacheKeyInputs, generation: DeviceGeneration
) -> str:
    """Convenience wrapper returning the device-resident realization key."""
    return inputs.device_realization_key(generation).key

__all__ = [
    "AdapterDeviceIdentity",
    "AsyncJobQueue",
    "CacheIdentityError",
    "CacheKey",
    "CacheKeyInputs",
    "CapabilityProfileIdentity",
    "CompletionDecision",
    "DeviceCacheKey",
    "DeviceEpoch",
    "DeviceEpochManager",
    "DeviceGeneration",
    "DeviceLostError",
    "GenerationToken",
    "GpuBudgetExceeded",
    "GpuCache",
    "GpuRealization",
    "GpuRealizationCache",
    "PreviewJobQueue",
    "SemanticCacheKey",
    "SemanticCompilerKey",
    "ToolchainIdentity",
    "canonical_json_bytes",
    "make_compiler_cache_key",
    "make_device_realization_key",
    "sha256_value",
]
