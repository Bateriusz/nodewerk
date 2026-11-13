#!/usr/bin/env python3
"""NW-05 compiler convenience surface.

This module is a stable import point for downstream tooling.  It re-exports
the whole-graph optimiser, logical/physical resource scheduler, and capability
profile exporter without defining another graph model, package format, or
runtime interpreter.
"""

from __future__ import annotations

from nw05_export import (  # noqa: F401
    ExportError,
    ExportResult,
    ExportSettings,
    artifact_key,
    canonical_profile_identity,
    embed_package,
    emitted_requirements,
    export,
    export_graph,
    extract_embedded_package,
    generate_player_source,
    inspect_size,
    inspect_size_report,
    plan_generation_vs_bake,
    section_sizes,
)
from nw05_optimizer import (  # noqa: F401
    CseRecord,
    FusionRecord,
    GraphOutput,
    IRNode,
    OptimizedGraph,
    OptimizationError,
    QuantizationDecision,
    QuantizedGraph,
    build_link_manifest,
    constant_propagation,
    dead_code_elimination,
    expression_to_text,
    fold_expression,
    lower_typed_graph,
    optimize_graph,
    parse_quantization_policy,
    quantize_document,
    quantize_parameters,
    recover_full_precision,
    semantic_outputs,
    shader_reachability,
)
from nw05_schedule import (  # noqa: F401
    AllocationRequirement,
    LogicalResource,
    LogicalSchedule,
    PhysicalRealization,
    ScheduleError,
    alias_compatible,
    build_logical_schedule,
    compatible_alias_group,
    lifetimes_overlap,
    logical_schedule,
    physical_alias_realization,
    realize_physical_aliases,
    schedule_digest,
    simulate_allocation,
)


def compile_export(graph_path, output_directory, **kwargs):
    """Compile/export a typed graph through the NW-05 pipeline."""
    return export_graph(graph_path, output_directory, **kwargs)


__all__ = [
    "ExportError",
    "ExportResult",
    "ExportSettings",
    "OptimizedGraph",
    "OptimizationError",
    "LogicalSchedule",
    "PhysicalRealization",
    "ScheduleError",
    "compile_export",
    "export_graph",
    "inspect_size",
    "optimize_graph",
    "build_logical_schedule",
    "realize_physical_aliases",
    "quantize_document",
    "recover_full_precision",
]

