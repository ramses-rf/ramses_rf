#!/usr/bin/env python3
"""A CLI for the ramses_rf library."""

from __future__ import annotations

from typing import Final

#
# NOTE: All debug flags should be False for deployment to end-users
_DBG_FORCE_CLI_DEBUGGING: Final[bool] = (
    False  # for debugging of CLI (usu. for click debugging)
)


if _DBG_FORCE_CLI_DEBUGGING:  # pragma: no cover
    from .debug import start_debugging

    start_debugging(True)
