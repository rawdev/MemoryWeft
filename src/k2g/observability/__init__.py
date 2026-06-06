"""Observability — logging / run_summary / cost reconciliation / plugin.

Phase A:
- ``logging_config`` — JSON Lines + dictConfig centralization
- ``run_summary`` — ``build_run_summary`` writer (BuildAudit pattern)

Phase C:
- ``balance_provider`` — Plugin Protocol + Snapshot
- ``registry`` — ``BalancePollerRegistry`` (llm_pool_meta.yaml + entry_points)
- ``calibration`` — Ridge regression (lazy)
- ``mcp_telemetry`` — FastMCP tool decorator wrapping
- ``metrics_endpoint`` — Prometheus exporter
- ``pollers/`` — 5 built-in pollers (4 light + Gemini lazy)
"""
