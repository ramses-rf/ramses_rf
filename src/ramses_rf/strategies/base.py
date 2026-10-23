"""RAMSES RF - HVAC vendor strategy base class.

Defines the :class:`HvacStrategy` protocol that each vendor-specific
strategy implements.  Vendor strategies own their fan mode maps,
payload quirks, binding codes, and classification heuristics.

See :doc:`roadmap item 5 <https://github.com/ramses-rf/ramses_rf/issues/1093>`.
"""

from __future__ import annotations

from typing import Protocol, cast, runtime_checkable

from ramses_rf.enums import DevType
from ramses_rf.exceptions import BindingError, DeviceNotFaked
from ramses_rf.models import HvacState
from ramses_rf.typing import SchemaT
from ramses_tx.const import Code, IndexT
from ramses_tx.typing import DeviceIdT


@runtime_checkable
class HvacStrategy(Protocol):
    """Vendor-specific HVAC behavior.

    Each strategy owns its vendor's fan mode mapping, payload quirks,
    binding codes, and classification heuristics.
    """

    #: Scheme name (e.g. ``"orcon"``, ``"climarad"``)
    scheme: str

    # --- Fan mode mapping ---

    def fan_mode_to_hex(self, fan_mode: str) -> str:
        """Map semantic fan mode name to hex code.

        :param fan_mode: The semantic fan mode name (e.g. ``"away"``).
        :type fan_mode: str
        :returns: The hex code (e.g. ``"00"``).
        :rtype: str
        :raises ValueError: If the fan mode is not valid for this scheme.
        """
        ...

    def hex_to_fan_mode(self, hex_code: str) -> str:
        """Map hex code to semantic fan mode name.

        :param hex_code: The hex code (e.g. ``"00"``).
        :type hex_code: str
        :returns: The semantic fan mode name (e.g. ``"away"``).
        :rtype: str
        """
        ...

    @property
    def mode_max(self) -> str | None:
        """Max mode byte for this scheme (e.g. ``"07"`` for orcon)."""
        ...

    @property
    def fan_modes(self) -> dict[str, str]:
        """All valid fan modes for this scheme (hex → name)."""
        ...

    @property
    def aliases(self) -> dict[str, str]:
        """Vendor fan-mode aliases (alias → canonical name)."""
        ...

    @property
    def boost_aliases(self) -> dict[str, str]:
        """Vendor boost-timer aliases (alias → canonical name)."""
        ...

    # --- Payload quirks ---

    def apply_quirk(
        self,
        payload: SchemaT,
        current_state: HvacState | None,
        msg_code: Code | str,
    ) -> SchemaT:
        """Apply vendor-specific payload transformations.

        :param payload: The flattened, canonical telemetry dictionary.
        :type payload: SchemaT
        :param current_state: The existing Read-Model for the device.
        :type current_state: HvacState | None
        :param msg_code: The hex opcode of the incoming message.
        :type msg_code: Code | str
        :returns: The safely mutated telemetry dictionary.
        :rtype: SchemaT
        """
        ...

    # --- Binding ---

    def binding_codes(self) -> tuple[Code, ...]:
        """Codes to send during binding process.

        :returns: Tuple of binding codes for this vendor.
        :rtype: tuple[Code, ...]
        """
        ...

    @property
    def builtin_commands(self) -> dict[str, dict[str, str]]:
        """Vendor-specific commands hardcoded in the strategy.

        Each entry is a command name → dict template (with ``verb``,
        ``code``, ``payload``, and ``type`` keys).  The ``type`` field
        classifies the command (``"mode"``, ``"bypass"``, ``"filter"``,
        etc.) so consumers can filter which commands appear in which
        UI context.

        :returns: Dict of command name → template.
        :rtype: dict[str, dict[str, str]]
        """
        ...

    def boost_command_name(self, code: Code | str, payload: str) -> str | None:
        """Canonical builtin boost-timer name for an observed payload.

        Structural match: strategies that support 22F3-style timed
        boosts map a received payload to one of their
        ``builtin_commands`` names (e.g. ``"high_15"``), tolerating
        per-model dialect differences in the non-semantic bytes.

        :param code: The opcode of the inbound frame.
        :type code: Code | str
        :param payload: The payload hex string.
        :type payload: str
        :returns: The canonical command name, or None if the frame is
            not a recognizable boost command for this vendor.
        :rtype: str | None
        """
        ...


@runtime_checkable
class VentilationControlStrategy(Protocol):
    """Capability interface for CO2 binding and demand control."""

    def co2_binding_codes(
        self,
    ) -> tuple[Code | tuple[IndexT, Code], ...]:
        """Codes and domains offered by a CO2 sensor.

        :returns: Ordered CO2 binding codes, optionally with domain indices.
        :rtype: tuple[Code | tuple[IndexT, Code], ...]
        """
        ...

    def ventilation_demand_payload(self, value: float) -> str:
        """Encode a ventilation demand for this device capability.

        :param value: Demand as a value from 0.0 to 1.0.
        :type value: float
        :returns: Encoded 31E0 payload.
        :rtype: str
        :raises ValueError: If this strategy does not support demand control.
        """
        ...


class HvacStrategyBase:
    """Base implementation with all-vendor normalisations.

    Vendor-specific strategies inherit from this class and override
    :meth:`apply_quirk` for vendor-specific behaviour.  The base
    class handles normalisations that apply to all vendors.
    """

    #: Scheme name — overridden by subclasses
    scheme: str = ""

    #: Mode map (hex → name) — overridden by subclasses
    _mode_map: dict[str, str] = {}

    #: Max mode byte — overridden by subclasses
    _mode_max: str | None = None

    #: Aliases (alias → canonical name).  Subclasses override with
    #: their own list, containing only canonical names that exist in
    #: their ``_mode_map``.  ``fan_mode_to_hex()`` resolves aliases
    #: before looking up the hex code, so aliases are always accepted
    #: regardless of UI language.
    _aliases: dict[str, str] = {}

    #: Dutch translations for common mode names.  Subclasses use this
    #: as a source to build their own ``_aliases`` by picking only the
    #: entries whose canonical name exists in their ``_mode_map``.
    _DUTCH_ALIASES: dict[str, str] = {
        "laag": "low",
        "hoog": "high",
        "gemiddeld": "medium",
        "midden": "medium",
        "minimaal": "trickle",
        "uit": "off",
        "afwezig": "away",
        "normaal": "normal",
        "boost": "boost",  # same in Dutch
        "auto": "auto",  # same in Dutch
    }

    #: Language code for the aliases (e.g. ``"nl"`` for Dutch), or
    #: ``None`` when aliases are language-neutral.  Consumers use this
    #: to decide whether to expose aliases in a localised UI.
    alias_language: str | None = None

    #: Binding codes — overridden by subclasses
    _binding_codes: tuple[Code, ...] = (Code._22F1, Code._22F3)

    # --- Fan mode mapping ---

    def fan_mode_to_hex(self, fan_mode: str) -> str:
        """Map semantic fan mode name to hex code.

        Accepts both canonical names and Dutch aliases.

        :param fan_mode: The semantic fan mode name.
        :type fan_mode: str
        :returns: The hex code.
        :rtype: str
        :raises ValueError: If the fan mode is not valid for this scheme.
        """
        # Resolve alias to canonical name
        canonical = self._aliases.get(fan_mode, fan_mode)
        reverse_map = {v: k for k, v in self._mode_map.items()}
        if canonical in reverse_map:
            return reverse_map[canonical]
        raise ValueError(
            f"fan_mode is not valid for scheme '{self.scheme}': {fan_mode}"
        )

    def hex_to_fan_mode(self, hex_code: str) -> str:
        """Map hex code to canonical semantic fan mode name.

        :param hex_code: The hex code.
        :type hex_code: str
        :returns: The canonical semantic fan mode name.
        :rtype: str
        """
        return self._mode_map.get(hex_code, hex_code)

    @property
    def mode_max(self) -> str | None:
        """Max mode byte for this scheme."""
        return self._mode_max

    @property
    def fan_modes(self) -> dict[str, str]:
        """All valid fan modes for this scheme (hex → name)."""
        return dict(self._mode_map)

    @property
    def aliases(self) -> dict[str, str]:
        """Vendor fan-mode aliases (alias → canonical name).

        Aliases (e.g. Dutch ``laag`` for ``low``) are always accepted
        by :meth:`fan_mode_to_hex`; this accessor exposes them for
        consumers that display localised mode names.
        """
        return dict(self._aliases)

    @property
    def boost_aliases(self) -> dict[str, str]:
        """Vendor boost-timer aliases (alias → canonical name).

        Aliases for the timed boost commands in
        :attr:`builtin_commands` (e.g. ``laag_15`` for ``low_15``).
        """
        return dict(self._boost_aliases)

    # --- Payload quirks ---

    def apply_quirk(
        self,
        payload: SchemaT,
        current_state: HvacState | None,
        msg_code: Code | str,
    ) -> SchemaT:
        """Apply all-vendor normalisations.

        Vendor-specific quirks are handled by subclass overrides
        which call ``super().apply_quirk()`` first, then add
        vendor-specific logic.

        :param payload: The flattened, canonical telemetry dictionary.
        :type payload: SchemaT
        :param current_state: The existing Read-Model for the device.
        :type current_state: HvacState | None
        :param msg_code: The hex opcode of the incoming message.
        :type msg_code: Code | str
        :returns: The safely mutated telemetry dictionary.
        :rtype: SchemaT
        """
        mutated = dict(payload)

        # QUIRK: 31DA humidity 0.0 → None (null-marker normalisation)
        # Some devices (e.g. Ventura V1x) send 0x00 for indoor/outdoor
        # humidity in 31DA when no sensor is present. This parses as 0.0
        # (physically impossible on Earth). Normalise to None so both
        # ingestion paths (dispatcher and StateProjector) filter it out.
        # See ramses_cc#742.
        if msg_code == Code._31DA:
            if mutated.get("indoor_humidity") == 0.0:
                mutated["indoor_humidity"] = None
            if mutated.get("outdoor_humidity") == 0.0:
                mutated["outdoor_humidity"] = None

        # QUIRK: 31D9 raw-hex fan_mode → None (semantic-value preservation)
        # For long-payload devices (Orcon, Brofer, etc.), the 31D9 parser
        # sets fan_mode = raw_payload[4:6] — a raw hex byte like "04", "C8",
        # "FF". These are NOT semantic names and conflict with the semantic
        # fan_mode from 22F4 ("off", "paused", "auto", "manual") or 22F1
        # (scheme-specific names like "away", "low", "high", "boost"). The
        # raw hex overwrites the good semantic value every 31D9 broadcast
        # cycle, causing fan_mode to toggle (e.g. "auto" ↔ "04").
        #
        # Vasco/ClimaRad short payloads (msg.len == 3) are already
        # converted to semantic strings by the parser's
        # _31D9_FAN_INFO_VASCO lookup, so they won't match the hex
        # pattern and are preserved.
        #
        # Drop any fan_mode that is a 2-char hex string (raw byte). The
        # authoritative semantic fan_mode comes from 22F4 (polled) or
        # 22F1 (command reply). See ramses_cc issue 723.
        if msg_code == Code._31D9 and "fan_mode" in mutated:
            mode_value = mutated["fan_mode"]
            if isinstance(mode_value, str) and len(mode_value) == 2:
                try:
                    int(mode_value, 16)  # is it a raw hex byte?
                    mutated["fan_mode"] = None
                except ValueError:
                    pass  # semantic string, keep it

        if not current_state:
            return mutated

        # QUIRK: 31DA 'fan_info' precedence over 22F1/22F4
        # 31DA snapshots include a fan_info byte that may be a null marker
        # or an unknown code for devices that report fan state via
        # 22F1/22F4 instead.  We must not overwrite a valid, rich string
        # from 22F1/22F4/31D9 with:
        #   - "" or "off"  (blank/null markers)
        #   - "-unknown 0xNN-"  (unrecognised codes, e.g. Ventura's 0x1F)
        if msg_code == Code._31DA and "fan_info" in mutated:
            incoming = mutated["fan_info"]
            if incoming in ("off", "") or (
                isinstance(incoming, str) and incoming.startswith("-unknown")
            ):
                if (
                    current_state.fan_info
                    and current_state.fan_info
                    not in (
                        "off",
                        "",
                    )
                    and not current_state.fan_info.startswith("-unknown")
                ):
                    mutated["fan_info"] = current_state.fan_info

        return mutated

    # --- Binding ---

    def binding_codes(self) -> tuple[Code, ...]:
        """Codes to send during binding process.

        :returns: Tuple of binding codes for this vendor.
        :rtype: tuple[Code, ...]
        """
        return self._binding_codes

    def co2_binding_codes(
        self,
    ) -> tuple[Code | tuple[IndexT, Code], ...]:
        """Return the generic CO2 sensor binding offer."""
        return (Code._31E0, Code._1298, Code._2E10)

    async def unbind_remote(self, fan: object, device_id: str) -> bool:
        """Release the FAN's binding slot for a REM/DIS over RF.

        Contract stub for ramses_cc issue 1283: on-air unbind is
        vendor/device-dependent and is only enabled on strategies whose
        sequence has been verified against real hardware.  Callers must
        treat a False result as "no on-air unbind happened".

        :param fan: The bound FAN device (``HvacVentilator``).
        :type fan: object
        :param device_id: The REM/DIS id to unbind on-air.
        :type device_id: str
        :return: True only when the unbind handshake completed.
        :rtype: bool
        """
        return False

    async def _send_unbind_handshake(
        self, fan: object, device_id: str
    ) -> bool:
        """Run the 1FC9 unbind handshake impersonating the bound child.

        A hardware capture of the device-side unbind gesture (a long
        button press, ramses_cc issue 1283) shows unbinding is the
        standard 3-way 1FC9 handshake — Offer → Accept → Confirm —
        where the Confirm carries *no* codes: the ``00`` payload
        confirms zero bindings and releases the FAN's slot.  The offer
        is the child's usual binding codes (CO2 sensors use
        :meth:`co2_binding_codes`).

        Note the gesture is *device-initiated*: real battery REM/DIS
        units may instead just drop the binding on their own side and
        emit no 1FC9 traffic at all, in which case the FAN keeps the
        registration until its bound list is wiped.  A silent RF side
        is valid hardware behaviour — callers should not treat a
        missing handshake as a failed unbind.

        Requires the child to be a Fakeable device — the gateway signs
        the frames as the child, exactly like ``bind_device``.

        :param fan: The bound FAN device (``HvacVentilator``).
        :type fan: object
        :param device_id: The REM/DIS/CO2 id to unbind on-air.
        :type device_id: str
        :return: True when the empty Confirm completed the handshake.
        :rtype: bool
        """
        gateway = getattr(fan, "_gateway", None)
        registry = getattr(gateway, "device_registry", None)
        device_by_id = getattr(registry, "device_by_id", None)
        child = (
            device_by_id.get(cast(DeviceIdT, device_id))
            if isinstance(device_by_id, dict)
            else None
        )
        fsm = getattr(child, "initiate_binding_process_with", None)
        if child is None or not callable(fsm):
            return False

        offer = (
            self.co2_binding_codes()
            if getattr(child, "_SLUG", None) == DevType.CO2
            else self.binding_codes()
        )
        try:
            await fsm(offer, confirm_code=None)
        except (BindingError, DeviceNotFaked):
            return False
        return True

    def ventilation_demand_payload(self, value: float) -> str:
        """Reject ventilation demand when the capability is unknown."""
        raise ValueError(
            f"ventilation demand is not supported for scheme '{self.scheme}'"
        )

    #: Vendor-specific commands hardcoded in the strategy.
    #: Subclasses override with vendor-specific commands (bypass, filter
    #: reset, etc.).  Each entry is a command name → dict template with
    #: ``verb``, ``code``, ``payload``, and ``type`` keys.
    #:
    #: The ``type`` field classifies the command so consumers can filter
    #: which commands appear in which UI context (e.g. only ``"mode"``
    #: commands in the fan_modes dropdown).  Valid types:
    #:
    #: - ``"mode"``        — 22F1: fan speed modes (away, low, high, boost, ...)
    #: - ``"config"``      — 2411: configuration parameters (fan rates %, filter
    #:                       time, moisture sensitivity, comfort temp, ...)
    #: - ``"bypass"``      — 22F7: bypass valve control (auto, open, closed)
    #: - ``"boost_timer"`` — 22F3: timed boost (high for N minutes)
    #: - ``"info"``        — 10D0, 31DA: filter status/reset, ventilation state
    #: - ``"other"``       — anything not in the standard code map
    _builtin_commands: dict[str, dict[str, str]] = {}

    #: Dutch aliases for boost_timer commands (alias → canonical
    #: name).  Subclasses build their own from this source, picking
    #: only entries whose canonical name exists in their
    #: ``_builtin_commands``.  Consumers use these to expose
    #: localised preset names in the UI (e.g. ``laag_15`` for
    #: ``low_15``).
    _DUTCH_BOOST_ALIASES: dict[str, str] = {
        "laag_15": "low_15",
        "laag_30": "low_30",
        "laag_60": "low_60",
        "middel_15": "medium_15",
        "middel_30": "medium_30",
        "middel_60": "medium_60",
        "hoog_15": "high_15",
        "hoog_30": "high_30",
        "hoog_60": "high_60",
    }

    #: Boost timer aliases (alias → canonical name).  Subclasses
    #: override with their own dict, containing only canonical names
    #: that exist in their ``_builtin_commands``.
    _boost_aliases: dict[str, str] = {}

    @property
    def builtin_commands(self) -> dict[str, dict[str, str]]:
        """Vendor-specific commands hardcoded in the strategy.

        Each entry is a command name → dict template (with ``verb``,
        ``code``, ``payload``, and ``type`` keys).  The ``type`` field
        classifies the command (``"mode"``, ``"bypass"``, ``"filter"``,
        etc.) so consumers can filter which commands appear in which
        UI context.

        Base implementation returns an empty dict — subclasses override
        to add vendor-specific commands.

        :returns: Dict of command name → template.
        :rtype: dict[str, dict[str, str]]
        """
        return dict(self._builtin_commands)

    def boost_command_name(self, code: Code | str, payload: str) -> str | None:
        """Canonical builtin boost-timer name for an observed payload.

        Structural match: strategies that support 22F3-style timed
        boosts map a received payload to one of their
        ``builtin_commands`` names (e.g. ``"high_15"``), tolerating
        per-model dialect differences in the non-semantic bytes.

        :param code: The opcode of the inbound frame.
        :type code: Code | str
        :param payload: The payload hex string.
        :type payload: str
        :returns: The canonical command name, or None if the frame is
            not a recognizable boost command for this vendor.
        :rtype: str | None
        """
        return None
