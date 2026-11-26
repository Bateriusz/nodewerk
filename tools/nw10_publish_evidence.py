"""Compatibility entry point. Use compact_export_evidence_publisher for new code."""
import sys
import runpy

if __name__ == "__main__":
    runpy.run_module("compact_export_evidence_publisher", run_name="__main__")
else:
    import compact_export_evidence_publisher as implementation
    sys.modules[__name__] = implementation
