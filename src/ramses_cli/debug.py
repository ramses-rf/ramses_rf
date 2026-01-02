#!/usr/bin/env python3
"""A CLI for the ramses_rf library."""

from __future__ import annotations

SZ_DBG_MODE = "debug_mode"
DEBUG_ADDR = "0.0.0.0"
DEBUG_PORT = 5678


def start_debugging(wait_for_client: bool) -> None:
    import debugpy  # type: ignore[import-untyped]

    debugpy.listen(address=(DEBUG_ADDR, DEBUG_PORT))
    print(f" - Debugging is enabled, listening on: {DEBUG_ADDR}:{DEBUG_PORT}")

    if wait_for_client:
        print("   - execution paused, waiting for debugger to attach...")
        debugpy.wait_for_client()
        print("   - debugger is now attached, continuing execution.")
