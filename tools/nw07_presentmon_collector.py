"""Compatibility entry point. Use presentation_metrics for new code."""
import sys
import runpy

if __name__ == "__main__":
    runpy.run_module("presentation_metrics", run_name="__main__")
else:
    import presentation_metrics as implementation
    sys.modules[__name__] = implementation
