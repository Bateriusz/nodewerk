# NW-13 film inspection

The default hosted proof still runs the 48×27 hotel-corridor fixture from
`fixtures/nw13/hotel_corridor.production.json`. The independently named
`fixtures/nw13/hotel_corridor.inspection.production.json` targets a native
320×180 raster (57,600 shaded output pixels, about 44× the original). Both
profiles use the same scene, solids, camera, shots, action and cue timing, song,
effect settings and typed History. The inspection graph is regenerated from
the same source documents, not an upscale or a second authored scene.

On a pinned Windows export host, the existing artifact measure entry point
selects the inspection source explicitly:

```powershell
$probe = (Get-ChildItem build/nw13-probe -Recurse -Filter nw11_capability_probe.exe |
  Select-Object -First 1).FullName
if (-not $probe) { throw "Build the pinned NW-11 capability probe first." }
$dxc = (Get-Command dxc.exe -ErrorAction Stop).Source
python tools/nw13_artifact_measure.py `
  --production fixtures/nw13/hotel_corridor.inspection.production.json `
  --output-dir build/nw13-inspection `
  --template build/nw07-release/staging/procedural_image_player/nw_procedural_image_player.exe `
  --probe "$probe" `
  --dxc "$dxc"
```

This full native check includes CPU-reference comparisons and may take longer
than the small hosted proof. For visual inspection, use the exact delivered
inspection standalone with `--maximized`. This requests a visible Windows
window sized independently of the film raster; it does not change shader
dispatch, qualify a display, or make the film native Full HD. Without that
switch, the existing native-client and borderless behavior remains unchanged.
On appropriate hardware, collect physical evidence with the shared validation
procedure. Compare common and optional DXR at the **same** native extent,
presentation time, settings and adapter. Do not compare the verification
raster with the inspection raster as if they were matched performance arms.

## Admission and evidence

The authored raster and any NW-05 graph axes must agree. The compiler derives
an exact 2D 8×8-thread dispatch; an override with a different size, missing
coverage, or extra groups is invalid. This contract applies before DXC and
allocation, including the direct stateless compiler, Studio reference
preview, stateful export and optional DXR. Typed History needs the full graph
exporter, not the direct shader-only `compile-package` path. The emitted
link manifest records the admitted `film_workload` beside artifact identity.
This is separate from package format, runtime allocations and hardware
measurements. None of those admission rules are a distribution byte gate.

Inspection initializes each object's frame once per workgroup in shared
memory instead of once per lane. The shader synchronizes before a lane traces
any object; the common and optional paths use the same prepared transforms.
The existing shared bloom tile, shadow/reflection paths, tonemap, particle
reconstruction, previous/current typed History and opaque presentation stay
enabled. The admission model counts complete rounded dispatches and clipped
halo samples (including overlaps), bounded shadow/reflection rays and field
normal probes, particle iterations, bloom/tonemap, and both History sample
passes plus composition and copies. Extra work inside a driver's ray
traversal is not an instruction/time measurement.

Run the source-bound static report:

```text
python tools/nw13_production.py --production fixtures/nw13/hotel_corridor.inspection.production.json
```

| Native raster | Profile | Static work units | Decision for this scene |
| --- | --- | ---: | --- |
| 48×27 | verification-v1 | 137,133,840 | Admitted, 150,000,000 limit |
| 160×90 | inspection-v1 | 1,569,499,392 | Projected admissible |
| 320×180 | inspection-v1 | 6,309,401,344 | Admitted, 6,500,000,000 limit |
| 512×288 | inspection-v1 | 16,164,113,184 | Over this profile's work limit |
| 640×360 | inspection-v1 | 25,300,412,928 | Over work and package raster limits |
| 1280×720 | inspection-v1 | 101,553,964,128 | Over work and package raster limits |
| 1920×1080 | inspection-v1 | 228,760,936,128 | Over work and package raster limits |

The inspection budget admits the chosen 320×180 workload with a bounded
margin rather than relaxing the original CI budget; it gives no frame-rate
or GPU-time guarantee. The 320×180 RGBA32F image surface occupies 921,600
logical bytes, not the amount of committed VRAM. Startup, resource/AS
allocation, CPU/GPU timing and native image tolerance are independent
observations. The two profiles have not been physically qualified by this
static report.
