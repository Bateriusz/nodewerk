"""Compatibility entry point. Use validation_gpu_telemetry for new code."""
import sys
import runpy

if __name__ == "__main__":
    runpy.run_module("validation_gpu_telemetry", run_name="__main__")
else:
    import validation_gpu_telemetry as implementation
    sys.modules[__name__] = implementation
