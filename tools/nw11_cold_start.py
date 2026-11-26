"""Compatibility entry point. Use validation_cold_start for new code."""
import sys
import runpy

if __name__ == "__main__":
    runpy.run_module("validation_cold_start", run_name="__main__")
else:
    import validation_cold_start as implementation
    sys.modules[__name__] = implementation
