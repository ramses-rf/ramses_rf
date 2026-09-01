"""RAMSES RF - HVAC vendor strategy base class.

Defines the :class:`HvacStrategy` protocol that each vendor-specific
strategy implements.  Vendor strategies own their fan mode maps,
payload quirks, binding codes, and classification heuristics.

See :doc:`roadmap item 5 <https://github.com/ramses-rf/ramses_rf/issues/1093>`.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ramses_rf.models import HvacState
from ramses_tx.const import Code


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

    def normalize_fan_info(self, fan_info: str) -> str:
        """Normalise a 31DA fan_info string to a canonical mode name.

        The 31DA parser produces descriptive strings like
        ``"speed 1, low"`` that don't match the canonical mode names
        (``"low"``) used in 22F1 mode maps and HA dropdowns.  This
        method maps descriptive names back to canonical names so
        consumers can compare fan_info against fan_modes.

        :param fan_info: The descriptive fan_info string from 31DA.
        :type fan_info: str
        :returns: The canonical mode name, or the original string if
            no mapping is known.
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

    # --- Payload quirks ---

    def apply_quirk(
        self,
        payload: dict[str, Any],
        current_state: HvacState | None,
        msg_code: Code | str,
    ) -> dict[str, Any]:
        """Apply vendor-specific payload transformations.

        :param payload: The flattened, canonical telemetry dictionary.
        :type payload: dict[str, Any]
        :param current_state: The existing Read-Model for the device.
        :type current_state: HvacState | None
        :param msg_code: The hex opcode of the incoming message.
        :type msg_code: Code | str
        :returns: The safely mutated telemetry dictionary.
        :rtype: dict[str, Any]
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

    #: Descriptive 31DA fan_info strings → canonical mode names.
    #: The 31DA parser uses ``_31DA_FAN_INFO`` which produces
    #: descriptive strings (e.g. ``"speed 1, low"``).  These don't
    #: match the canonical names in ``_mode_map`` (e.g. ``"low"``)
    #: that 22F1 uses and that HA dropdowns display.  This map
    #: normalises 31DA fan_info to canonical names so consumers can
    #: compare fan_info against fan_modes.  See ramses_cc issue 1116.
    _FAN_INFO_NORMALISE: dict[str, str] = {
        "speed 1, low": "low",
        "speed 2, medium": "medium",
        "speed 3, high": "high",
    }

    def normalize_fan_info(self, fan_info: str) -> str:
        """Normalise a 31DA fan_info string to a canonical mode name.

        :param fan_info: The descriptive fan_info string from 31DA.
        :type fan_info: str
        :returns: The canonical mode name, or the original string if
            no mapping is known.
        :rtype: str
        """
        return self._FAN_INFO_NORMALISE.get(fan_info, fan_info)

    @property
    def mode_max(self) -> str | None:
        """Max mode byte for this scheme."""
        return self._mode_max

    @property
    def fan_modes(self) -> dict[str, str]:
        """All valid fan modes for this scheme (hex → name)."""
        return dict(self._mode_map)

    # --- Payload quirks ---

    def apply_quirk(
        self,
        payload: dict[str, Any],
        current_state: HvacState | None,
        msg_code: Code | str,
    ) -> dict[str, Any]:
        """Apply all-vendor normalisations.

        Vendor-specific quirks are handled by subclass overrides
        which call ``super().apply_quirk()`` first, then add
        vendor-specific logic.

        :param payload: The flattened, canonical telemetry dictionary.
        :type payload: dict[str, Any]
        :param current_state: The existing Read-Model for the device.
        :type current_state: HvacState | None
        :param msg_code: The hex opcode of the incoming message.
        :type msg_code: Code | str
        :returns: The safely mutated telemetry dictionary.
        :rtype: dict[str, Any]
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
            # Still normalise fan_info even without current state.
            if msg_code == Code._31DA and "fan_info" in mutated:
                incoming = mutated["fan_info"]
                if isinstance(incoming, str):
                    mutated["fan_info"] = self.normalize_fan_info(incoming)
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
            elif isinstance(incoming, str):
                # Normalise descriptive 31DA fan_info (e.g.
                # "speed 1, low") to canonical mode names (e.g.
                # "low") so consumers can match against fan_modes.
                # See ramses_cc issue 1116.
                mutated["fan_info"] = self.normalize_fan_info(incoming)

        return mutated

    # --- Binding ---

    def binding_codes(self) -> tuple[Code, ...]:
        """Codes to send during binding process.

        :returns: Tuple of binding codes for this vendor.
        :rtype: tuple[Code, ...]
        """
        return self._binding_codes

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
