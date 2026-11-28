# NW-13 fixed-tick image evidence

These PPM frames are the actual output of the NW-13 CPU reference renderer for
the retained hotel-room/corridor production at the declared fixed ticks. They
cover the corridor approach, the shot cut, the door and flash cues, and the
room-turn camera motion around frozen action.

`frames.v1.json` records the source hashes, per-frame SHA-256, shot identity,
region luminance checks, alpha/depth conventions and declared tolerances.
The reference trace marks the selected raw History predecessor, or null at
cut/reset boundaries. Hosted GPU qualification separately compares the live
feedback and zero-weight control artifacts.
`contact_sheet.ppm` tiles the retained frames.
`transport.v1.json` records the fixed-tick transport trace, including the
declared freeze/reverse/resume/cut states and one-shot cue de-duplication.
The editable graph is the actual rendering source; companion scene and
geometry JSON files are labeled as import inputs only.
Both manifests have `.sha256` sidecars binding their exact retained bytes.

Regenerate only through `tools/nw13_production.py` so the manifest hashes and
the evidence files stay bound to the same production inputs. Missing physical
GPU/display/audio observations are not inferred from these images.

