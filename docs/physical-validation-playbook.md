# Physical validation playbook

This is the repository-wide contract for Windows/NVIDIA physical validation.
Tasks that require physical GPU evidence must reuse this playbook and the shared
host-evidence code instead of reimplementing host/VM detection, Windows text
decoding, or evidence identity rules locally.

The shared implementation is:

- `tools/hardware_evidence_host.py`
- `hardware_evidence_host.windows_virtualization_probe()`
- `hardware_evidence_host.evaluate_physical_virtualization_probe()`
- `hardware_evidence_host.decode_windows_output()`
- `hardware_evidence_host.parse_pci_hex()`

NW-07 clean-system validation and NW-10 evidence publication consume this
policy. NW-11 physical experiment validation consumes the same policy and adds
only experiment-specific NVIDIA/D3D12 checks.

## Lessons retained from NW-04, NW-07, NW-10 and NW-11

### Physical Windows hosts can report an active hypervisor

`Win32_ComputerSystem.HypervisorPresent=true` is not sufficient evidence that
the measurement is running in a VM. A physical Windows host can have the
Microsoft hypervisor active because Virtualization Based Security is enabled.

Physical-host classification therefore fails closed on a complete identity
probe, rejects known VM/cloud identity markers and generic OEM placeholders,
and accepts an active hypervisor only when the independently queried
`Win32_DeviceGuard.VirtualizationBasedSecurityStatus` reports VBS running.
Task-local code must not restore the shortcut
`HypervisorPresent == true => virtual machine`.

A task may additionally validate device-specific virtualization state. For
NVIDIA evidence, a physical local GPU is expected to report
`gpu_virtualization_mode/virtualization_mode = None`. A generic VM or
documented GPU-passthrough flow is a separate evidence class and must not be
silently relabeled physical.

### Windows command output is locale-dependent

Do not use `subprocess.run(..., text=True)` for evidence-critical Windows
utilities and then rely on Python's current locale. Polish and other localized
Windows installations can expose OEM/ANSI output that is not decodable with
the process ANSI code page.

Use `hardware_evidence_host.run_windows_text()` or
`decode_windows_output()`. The shared decoder handles BOM-marked Unicode,
UTF-8, and explicit Windows OEM/ANSI code pages without consulting Python
locale.

Evidence parsers should depend on stable identifiers, GUIDs and structured
output rather than translated friendly names. For example, power-plan and PCIe
power parsing must key off GUIDs/hex values, not the localized labels printed
next to them.

### NVIDIA telemetry layouts can change between driver generations

Do not bind physical validation to one exact `nvidia-smi -q -x` XML nesting when
NVIDIA documents a field migration. Driver 610+ moved GPU power-limit fields
under the GPU Ceiling Power Limit subsection while retaining legacy XML
locations only temporarily.

Parsers must accept the documented old/new nesting for the same semantic signal,
while preserving the distinction between the currently enforced/current ceiling
and merely requested/default limits. Missing current/enforced telemetry remains
unavailable; do not promote a default/requested value to measured current state.

### Hardware identifiers have multiple textual representations

NVIDIA and Windows tools do not guarantee the same hexadecimal spelling.
For example an NVIDIA PCI device identifier may appear as either
`280310DE` or `0x280310DE`.

Normalize hexadecimal identifiers numerically with
`hardware_evidence_host.parse_pci_hex()`. Do not compare raw spelling.

GPU identity should be cross-checked across the D3D12 capability observation,
NVIDIA telemetry and Windows display/driver telemetry. Vendor/device/subsystem
mismatches are evidence failures, not missing coverage.

### Physical evidence is bound to immutable artifacts

A physical result may certify only the exact immutable validation bundle and
source commit it executed. Record and verify the source commit, bundle identity,
manifest hash and executable identities. Do not copy a newer script into an
older bundle, recalculate selected hashes by hand, or reuse a successful
measurement from a previous task/revision as if it certified new bytes.

When evidence tooling changes, rebuild the bundle and perform a fresh physical
run even if the native renderer EXE did not change. The validation tooling is
part of the trusted measured closure.

### Cold-start evidence has ordering requirements

When a task requires qualified cold initialization, enable the required process
creation audit before the run, reboot, and avoid launching the exact measured
executables before the validation launcher records their first use. Different
experiments that each require a qualified cold start should be run after
separate reboots.

A preflight failure that occurs before any measured renderer is launched does
not itself create a valid cold-start sample. Inspect the retained evidence and
fix the preflight rather than fabricating or relabeling the result.

### Present and timing evidence must measure the real path

Hosted WARP/headless CI is useful for command-path validation but cannot replace
a physical windowed Present requirement. PresentMon collection must be tied to
the intended executable/process and must retain enough raw timing evidence to
validate warm-up, duration, sample density and first-present semantics.

Do not infer physical performance from another GPU generation or normalize by
theoretical throughput unless a separately versioned contract explicitly
defines and validates that method.

### Clean-system and developer-runtime assumptions must be observed

Do not infer a clean system from assumptions about a particular host. Record
the actual dependency closure and environment. The presence or absence of a
debug CRT, SDK, DXC, build tools or other development components must be
measured according to the task's clean-system contract; host folklore is not
evidence.

PowerShell execution policy is also an environment property, not an artifact
failure. Trusted repository launchers may be invoked in a process-scoped
`-ExecutionPolicy Bypass` mode when required; do not change machine-wide
policy merely to execute a validation bundle.

## Required task-authoring rule

A new issue that requires physical Windows/GPU validation should explicitly
state:

> Reuse `docs/physical-validation-playbook.md` and the repository shared
> physical-host/evidence helpers. Do not introduce task-local host/VM
> classification, locale-dependent Windows subprocess decoding, PCI-ID parsing,
> Present/cold-start semantics, or artifact-binding rules unless the shared
> contract is intentionally being extended with regression tests.

If a task discovers a new reusable environmental failure mode, fix the shared
helper/playbook in the same change where practical. Do not leave the knowledge
only in a PR review comment or a task-specific script.

## Pre-run checklist

1. CI has produced the immutable validation bundle and trust record for the
   exact commit to be measured.
2. The bundle contains every shared validation module imported by the launcher.
3. Host identity, Hyper-V/VBS and GPU virtualization probes are available and
   use the shared fail-closed policy.
4. Windows subprocess output is decoded through the shared locale-independent
   path.
5. GPU PCI identity and driver/WDDM identity agree across the independent
   probes.
6. Power plan, PCIe power management, clocks, power limit and temperature are
   observed rather than operator-invented.
7. For cold-start qualification, audit prerequisites are enabled and the host
   has been rebooted without launching the measured executable afterward.
8. The physical run uses a real display/Present path whenever the acceptance
   contract requires it.
9. Evidence is written outside the immutable bundle and retains raw samples,
   exact artifact identities and the validator summary.
10. A measured rejection remains a rejection; unavailable coverage remains
    pending. Neither is rewritten as a pass.

These checks are a reusable baseline. Individual tasks may add stricter
experiment-specific requirements, but should not weaken or fork this shared
physical-validation contract.
