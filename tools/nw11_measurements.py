"""Compatibility entry point. Use gpu_experiment_measurements for new code."""
import sys
import runpy

if __name__ == "__main__":
    runpy.run_module("gpu_experiment_measurements", run_name="__main__")
else:
    import gpu_experiment_measurements as implementation
    sys.modules[__name__] = implementation
