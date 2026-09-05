#!/usr/bin/env python3
"""RAMSES RF - Typed routing contract for pre-serialization outbound routing.

This module defines the immutable boundary objects and enum used by the
transport-neutral routing API introduced in PR 2 (issue 1119).

The routing contract moves child selection and source-address resolution
to *before* frame serialization, replacing the post-serialization
``frame.split()`` approach that ``PooledTransport.write_frame()`` previously
used.

Key types:

- :class:`SourcePolicy` — whether the command source should be patched to
  the selected gateway HGI (``GATEWAY``) or left as-is (``PRESERVE``).
- :class:`RouteRequest` — immutable wrapper around a ``CommandDTO`` plus
  a ``SourcePolicy``.
- :class:`RoutedCommand` — the result of route preparation: a pinned
  child ID and the final ``CommandDTO`` (with source already resolved).
- :class:`WriteOutcome` — conservative classification of a write attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .dtos import CommandDTO


class SourcePolicy(Enum):
    """Source-address intent for an outbound command.

    ``GATEWAY``: the command source should be the selected gateway HGI.
    The transport may substitute the source address to match the
    selected child's HGI ID (evofw3) or leave the placeholder (HGI80).

    ``PRESERVE``: the command source must not be changed.  Used for
    faked-device commands that carry an intentional source address,
    including intentional ``18:`` sources.
    """

    GATEWAY = auto()
    PRESERVE = auto()


@dataclass(frozen=True, slots=True)
class RouteRequest:
    """Immutable request to prepare a command for routed transmission.

    Wraps the original :class:`CommandDTO` with a :class:`SourcePolicy`
    so the routing layer knows whether it may substitute the source
    address.
    """

    command: CommandDTO
    source_policy: SourcePolicy = SourcePolicy.GATEWAY


@dataclass(frozen=True, slots=True)
class RoutedCommand:
    """Immutable result of route preparation.

    Carries the pinned child ID and the final :class:`CommandDTO`
    (with source already resolved).  The protocol layer serializes
    this DTO once per QoS attempt and dispatches through
    ``write_routed()``.
    """

    child_id: str
    command: CommandDTO


class WriteOutcome(Enum):
    """Conservative classification of a write attempt.

    - ``SUBMITTED``: the write was accepted locally (bytes sent to OS
      or MQTT message handed to broker).  RF result is unknown until
      an exact echo or QoS timeout.
    - ``NOT_SUBMITTED``: proven that no bytes or MQTT message were
      handed to the local transport.  It is safe to prepare another
      child immediately.
    - ``AMBIGUOUS``: an exception occurred but the failure point is
      unclear (may have partially written).  Do not immediately send
      through another child.
    """

    SUBMITTED = auto()
    NOT_SUBMITTED = auto()
    AMBIGUOUS = auto()
