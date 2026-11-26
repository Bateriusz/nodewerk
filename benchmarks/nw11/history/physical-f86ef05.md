# Physical GPU execution — f86ef05 (2026-11-26)

Tested source revision: `f86ef053a11ac5178535ef9caeb2c1cb3ea0d86e`.
Protocol: v3 (three independent 60-second runs per arm, 5-second sampling
warm-up). CI bundle verification: [run 45829771204](https://github.com/Bateriusz/nodewerk/actions/runs/45829771204).

This is a sanitized projection of operator-provided console reports, not a
reconstructed raw evidence or cross-device decision. It contains no local
paths, names, UUIDs, process identities or machine serials. The complete
original bundle, raw reports, logs and fixed-tick captures remain on the
measurement hosts. They must be reverified before changing tracked
completion status. No result is assigned to a newer build.

## Tested bundles

| Experiment | Bundle identity | Manifest SHA-256 |
| --- | --- | --- |
| Mesh shading | sha256:v1:dcaa666a5dbfdb5cec068f236510e9f54b2c3158d90448336c66073828ac00b3 | fd4fd17c10a3d8fecbcf5e0ecbe3342ad7e6067265d1bf5a153e3933c286dd77 |
| Inline ray queries | sha256:v1:edc17d1e4d12eda9ed5b692dc141722e1531e2a72ca0e7411d53ed4cfffe64f1 | 3bfb46b96c43c9153726a6a013c78b41ee3b59a714d882ff6c18a8396bed029a |

## Ada — RTX 4060 Ti

Both experiments reported per-device `accepted`, completion true and
fallback tested true. The aggregate remained incomplete with one device.
The first ray query attempt failed the foreground-window observer; the
subsequent execution completed. The supplied console excerpts are truncated;
the original full files are the authoritative source.

| Experiment | Baseline p99 GPU ms | Variant p99 GPU ms | Worst paired reduction | Performance | Image |
| --- | ---: | ---: | ---: | --- | --- |
| Mesh shading | 16.025 | 16.0483 | -0.1454% | target not met | max error differed |
| Inline ray queries | 16.0252 | 16.0159 | 0.0580% | target not met | max error and RMSE differed |

## Blackwell — RTX 5070 Ti

Both experiment manifests passed verification. All eighteen native arm
processes reported `eligible`, exited with code 0 and reported nonzero
successful Present calls. Neither experiment produced the required ETW
CSV, so the original collector stopped with a missing-file error rather
than emitting a decision. For the mesh experiment all nine PresentMon
logs reported 12,216,090 to 12,734,493 lost ETW events. The exact Ray
Queries ETW loss count was not supplied. No display timing or delivered
frame count is inferred from renderer reports.

| Experiment | Role | Native Present counts (runs 1–3) | Startup ms (runs 1–3) | Engine GPU allocation bytes |
| --- | --- | --- | --- | ---: |
| Mesh | baseline | 225196, 224028, 222049 | 212.4249, 217.9941, 215.9347 | 115017088 |
| Mesh | variant | 230748, 228907, 228949 | 215.2534, 215.3337, 217.2276 | 115017088 |
| Mesh | fallback | 225175, 221328, 223618 | 216.1320, 223.7834, 220.7168 | 115017088 |
| Ray | baseline | 224334, 224825, 224481 | 484.2500, 214.8628, 214.4493 | 115017088 |
| Ray | variant | 111202, 112458, 112110 | 608.5367, 325.7306, 326.4841 | 115737984 |
| Ray | fallback | 224460, 224658, 224904 | 348.8229, 213.4812, 215.3638 | 115017088 |

The native results support successful functional execution on Blackwell;
ETW performance and cross-device canonical image verification remain
unavailable or pending until the actual raw records are processed.
All four operator-reported experiments are functionally positive; this
does not assert image equivalence, statistical speed gains, default
adoption, or independently verified aggregate completion. Recover the
original v3 measurements with the new repository-owned verifier rather
than re-executing the experiments or inventing missing CSVs.
