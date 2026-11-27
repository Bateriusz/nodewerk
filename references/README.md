# Reference Sources

The contents of this directory are included as reference material only.

Nodewerk is a modern clean-room implementation inspired by architectural ideas found in historical demoscene tools, particularly Farbrausch's operator-based workflow and the sources published in `fr_public`.

The reference code is intended to help study concepts such as:

- operator graphs and evaluation,
- procedural texture and mesh generation,
- separation between authoring tools and runtime/player code,
- parameterized content generation,
- size-conscious asset and scene construction.

Nodewerk is not intended to be a port, fork, or compatibility layer for the original Farbrausch codebase. New code should be designed for contemporary platforms, graphics APIs, tooling, and project requirements rather than copied or mechanically translated from the reference sources.

Each referenced project remains subject to its own license and copyright terms. Consult the license files in the corresponding submodule before reusing any source code.

## Operator inventory and coverage

The [operator catalog](operator_catalog/README.md) preserves source-linked
Werkkzeug3/Werkkzeug4 declarations and distinguishes them from nodewerk's
current capabilities and planned implementations. See its
[overview](operator_catalog/overview.md) for counts and state, and the
[operator roadmap](../docs/operator-roadmap.md) for the accepted NW-13 method.
Historical entries remain visible when a modern implementation replaces them;
the inventory does not require a literal port of every operator.

