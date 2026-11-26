#!/usr/bin/env python3
"""Compatibility entry point; use canonical_export_reproducibility."""
import sys
import canonical_export_reproducibility as implementation

if __name__ == "__main__":
    raise SystemExit(implementation.main())
sys.modules[__name__] = implementation
