#!/usr/bin/env python3
"""RAMSES RF - Unittests for HVAC vendor strategies.

Tests cover:
- Fan mode mapping (hex ↔ name) for each vendor strategy
- Dutch aliases for Orcon (bug ramses_cc#995)
- Binding codes per vendor
- mode_max per vendor
- Strategy selection via scheme name
"""

from __future__ import annotations

import pytest

from ramses_rf.const import SZ_REL_HUMIDITY
from ramses_rf.quirks import apply_hvac_quirks
from ramses_rf.strategies import (
    _STRATEGY_BY_SCHEME,
    ClimaRadStrategy,
    HvacStrategy,
    IthoStrategy,
    NuaireStrategy,
    OrconHrc350Strategy,
    OrconStrategy,
    VascoStrategy,
    best_hvac_strategy,
)
from ramses_rf.strategies.base import HvacStrategyBase
from ramses_tx.const import Code

# ---------------------------------------------------------------------------
# Fan mode mapping
# ---------------------------------------------------------------------------


class TestOrconFanModes:
    """Orcon fan mode mapping."""

    @pytest.fixture()
    def strategy(self) -> OrconStrategy:
        return OrconStrategy()

    def test_hex_to_fan_mode(self, strategy: OrconStrategy) -> None:
        assert strategy.hex_to_fan_mode("00") == "away"
        assert strategy.hex_to_fan_mode("01") == "low"
        assert strategy.hex_to_fan_mode("06") == "boost"
        assert strategy.hex_to_fan_mode("07") == "off"

    def test_fan_mode_to_hex(self, strategy: OrconStrategy) -> None:
        assert strategy.fan_mode_to_hex("away") == "00"
        assert strategy.fan_mode_to_hex("low") == "01"
        assert strategy.fan_mode_to_hex("boost") == "06"
        assert strategy.fan_mode_to_hex("off") == "07"

    def test_mode_max(self, strategy: OrconStrategy) -> None:
        assert strategy.mode_max == "07"

    def test_fan_modes_dict(self, strategy: OrconStrategy) -> None:
        modes = strategy.fan_modes
        assert len(modes) == 8
        assert "00" in modes
        assert "07" in modes

    def test_invalid_fan_mode_raises(self, strategy: OrconStrategy) -> None:
        with pytest.raises(ValueError, match="not valid for scheme 'orcon'"):
            strategy.fan_mode_to_hex("nonexistent")


class TestIthoFanModes:
    """Itho fan mode mapping."""

    @pytest.fixture()
    def strategy(self) -> IthoStrategy:
        return IthoStrategy()

    def test_hex_to_fan_mode(self, strategy: IthoStrategy) -> None:
        assert strategy.hex_to_fan_mode("00") == "off"
        assert strategy.hex_to_fan_mode("01") == "trickle"
        assert strategy.hex_to_fan_mode("04") == "high"

    def test_fan_mode_to_hex(self, strategy: IthoStrategy) -> None:
        assert strategy.fan_mode_to_hex("off") == "00"
        assert strategy.fan_mode_to_hex("high") == "04"

    def test_mode_max(self, strategy: IthoStrategy) -> None:
        assert strategy.mode_max == "04"


class TestNuaireFanModes:
    """Nuaire fan mode mapping."""

    @pytest.fixture()
    def strategy(self) -> NuaireStrategy:
        return NuaireStrategy()

    def test_hex_to_fan_mode(self, strategy: NuaireStrategy) -> None:
        assert strategy.hex_to_fan_mode("02") == "normal"
        assert strategy.hex_to_fan_mode("03") == "boost"
        assert strategy.hex_to_fan_mode("09") == "heater_off"
        assert strategy.hex_to_fan_mode("0A") == "heater_auto"

    def test_fan_mode_to_hex(self, strategy: NuaireStrategy) -> None:
        assert strategy.fan_mode_to_hex("normal") == "02"
        assert strategy.fan_mode_to_hex("boost") == "03"

    def test_mode_max(self, strategy: NuaireStrategy) -> None:
        assert strategy.mode_max == "0A"


class TestClimaRadFanModes:
    """ClimaRad fan mode mapping."""

    def test_minibox_uses_vasco_mode_map(self) -> None:
        strategy = ClimaRadStrategy()

        assert strategy.fan_mode_to_hex("off") == "00"
        assert strategy.fan_mode_to_hex("auto") == "05"
        assert strategy.mode_max == "06"


class TestVascoFanModes:
    """Vasco fan mode mapping."""

    @pytest.fixture()
    def strategy(self) -> VascoStrategy:
        return VascoStrategy()

    def test_hex_to_fan_mode(self, strategy: VascoStrategy) -> None:
        assert strategy.hex_to_fan_mode("00") == "off"
        assert strategy.hex_to_fan_mode("01") == "away"
        assert strategy.hex_to_fan_mode("05") == "auto"

    def test_fan_mode_to_hex(self, strategy: VascoStrategy) -> None:
        assert strategy.fan_mode_to_hex("off") == "00"
        assert strategy.fan_mode_to_hex("auto") == "05"

    def test_mode_max(self, strategy: VascoStrategy) -> None:
        assert strategy.mode_max == "06"


# ---------------------------------------------------------------------------
# Dutch aliases (bug ramses_cc#995)
# ---------------------------------------------------------------------------


class TestOrconDutchAliases:
    """Orcon Dutch fan mode aliases for bug ramses_cc#995."""

    @pytest.fixture()
    def strategy(self) -> OrconStrategy:
        return OrconStrategy()

    def test_dutch_away(self, strategy: OrconStrategy) -> None:
        assert strategy.fan_mode_to_hex("afwezig") == "00"

    def test_dutch_low(self, strategy: OrconStrategy) -> None:
        assert strategy.fan_mode_to_hex("laag") == "01"

    def test_dutch_high(self, strategy: OrconStrategy) -> None:
        assert strategy.fan_mode_to_hex("hoog") == "03"

    def test_dutch_off(self, strategy: OrconStrategy) -> None:
        assert strategy.fan_mode_to_hex("uit") == "07"

    def test_dutch_medium_is_canonical(self, strategy: OrconStrategy) -> None:
        # "medium" is the same in Dutch and English
        assert strategy.fan_mode_to_hex("medium") == "02"

    def test_hex_to_fan_mode_returns_canonical(
        self, strategy: OrconStrategy
    ) -> None:
        # Even if input was a Dutch alias, hex_to_fan_mode returns
        # the canonical English name
        hex_code = strategy.fan_mode_to_hex("afwezig")
        assert hex_code == "00"
        assert strategy.hex_to_fan_mode(hex_code) == "away"


# ---------------------------------------------------------------------------
# alias_language attribute
# ---------------------------------------------------------------------------


class TestAliasLanguage:
    """Verify alias_language is set and consistent across strategies."""

    @pytest.mark.parametrize(
        "strategy_cls,expected_lang",
        [
            (OrconStrategy, "nl"),
            (IthoStrategy, "nl"),
            (NuaireStrategy, "nl"),
            (VascoStrategy, "nl"),
            (ClimaRadStrategy, "nl"),
        ],
    )
    def test_alias_language_set(
        self, strategy_cls: type, expected_lang: str
    ) -> None:
        assert strategy_cls().alias_language == expected_lang

    def test_base_alias_language_is_none(self) -> None:
        assert HvacStrategyBase.alias_language is None


# ---------------------------------------------------------------------------
# Filtered aliases per strategy
# ---------------------------------------------------------------------------


class TestFilteredAliases:
    """Verify each strategy only has aliases for modes it supports."""

    @pytest.mark.parametrize(
        "strategy_cls",
        [
            OrconStrategy,
            IthoStrategy,
            NuaireStrategy,
            VascoStrategy,
            ClimaRadStrategy,
        ],
    )
    def test_aliases_resolve_to_existing_modes(
        self, strategy_cls: type
    ) -> None:
        strategy = strategy_cls()
        mode_names = set(strategy.fan_modes.values())
        for alias, canonical in strategy._aliases.items():
            assert canonical in mode_names, (
                f"{strategy.scheme}: alias '{alias}' -> '{canonical}' "
                f"but '{canonical}' not in mode_map"
            )

    def test_orcon_has_dutch_aliases(self) -> None:
        s = OrconStrategy()
        assert "laag" in s._aliases
        assert s._aliases["laag"] == "low"
        assert "afwezig" in s._aliases
        assert s._aliases["afwezig"] == "away"

    def test_itho_has_dutch_aliases(self) -> None:
        s = IthoStrategy()
        assert "laag" in s._aliases
        assert s._aliases["laag"] == "low"
        assert "uit" in s._aliases
        assert s._aliases["uit"] == "off"

    def test_itho_does_not_have_afwezig(self) -> None:
        # Itho has no "away" mode, so the Dutch alias should be absent
        s = IthoStrategy()
        assert "afwezig" not in s._aliases

    def test_nuaire_has_normaal(self) -> None:
        s = NuaireStrategy()
        assert "normaal" in s._aliases
        assert s._aliases["normaal"] == "normal"

    def test_nuaire_does_not_have_laag(self) -> None:
        # Nuaire has no "low" mode
        s = NuaireStrategy()
        assert "laag" not in s._aliases

    def test_vasco_has_dutch_aliases(self) -> None:
        s = VascoStrategy()
        assert "laag" in s._aliases
        assert "afwezig" in s._aliases

    def test_climarad_shares_vasco_aliases(self) -> None:
        # ClimaRad uses the same mode map as Vasco
        s = ClimaRadStrategy()
        v = VascoStrategy()
        assert s._aliases == v._aliases

    def test_dutch_aliases_accept_via_fan_mode_to_hex(self) -> None:
        # All strategies should accept their Dutch aliases
        for cls in [
            OrconStrategy,
            IthoStrategy,
            NuaireStrategy,
            VascoStrategy,
            ClimaRadStrategy,
        ]:
            s = cls()
            for alias in s._aliases:
                # Should not raise
                hex_code = s.fan_mode_to_hex(alias)
                assert isinstance(hex_code, str)
                assert len(hex_code) == 2


# ---------------------------------------------------------------------------
# Binding codes
# ---------------------------------------------------------------------------


class TestBindingCodes:
    """Binding codes per vendor strategy."""

    def test_climarad_binding_codes(self) -> None:
        codes = ClimaRadStrategy().binding_codes()

        assert Code._22F1 in codes
        assert Code._22F3 in codes

    def test_orcon_binding_codes(self) -> None:
        strategy = OrconStrategy()
        codes = strategy.binding_codes()
        assert Code._22F1 in codes
        assert Code._22F3 in codes

    def test_itho_binding_codes(self) -> None:
        strategy = IthoStrategy()
        codes = strategy.binding_codes()
        assert Code._22F1 in codes
        assert Code._22F3 in codes

    def test_nuaire_binding_codes(self) -> None:
        strategy = NuaireStrategy()
        codes = strategy.binding_codes()
        assert Code._22F1 in codes
        # Nuaire uses 22F1 only (no 22F3)
        assert Code._22F3 not in codes

    def test_vasco_binding_codes(self) -> None:
        strategy = VascoStrategy()
        codes = strategy.binding_codes()
        assert Code._22F1 in codes
        assert Code._22F3 in codes


# ---------------------------------------------------------------------------
# Strategy registry
# ---------------------------------------------------------------------------


class TestStrategyRegistry:
    """Strategy registry lookup by scheme name."""

    def test_climarad_scheme(self) -> None:
        assert _STRATEGY_BY_SCHEME["climarad"] is ClimaRadStrategy

    def test_orcon_scheme(self) -> None:
        assert _STRATEGY_BY_SCHEME["orcon"] is OrconStrategy

    def test_itho_scheme(self) -> None:
        assert _STRATEGY_BY_SCHEME["itho"] is IthoStrategy

    def test_nuaire_scheme(self) -> None:
        assert _STRATEGY_BY_SCHEME["nuaire"] is NuaireStrategy

    def test_vasco_scheme(self) -> None:
        assert _STRATEGY_BY_SCHEME["vasco"] is VascoStrategy

    def test_all_configured_schemes_registered(self) -> None:
        assert set(_STRATEGY_BY_SCHEME) == {
            "climarad",
            "itho",
            "nuaire",
            "orcon",
            "vasco",
        }


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestProtocolCompliance:
    """Verify all strategies implement the HvacStrategy protocol."""

    def test_climarad_is_hvac_strategy(self) -> None:
        assert isinstance(ClimaRadStrategy(), HvacStrategy)

    def test_orcon_is_hvac_strategy(self) -> None:
        assert isinstance(OrconStrategy(), HvacStrategy)

    def test_itho_is_hvac_strategy(self) -> None:
        assert isinstance(IthoStrategy(), HvacStrategy)

    def test_nuaire_is_hvac_strategy(self) -> None:
        assert isinstance(NuaireStrategy(), HvacStrategy)

    def test_vasco_is_hvac_strategy(self) -> None:
        assert isinstance(VascoStrategy(), HvacStrategy)


# ---------------------------------------------------------------------------
# Scheme name
# ---------------------------------------------------------------------------


class TestSchemeName:
    """Verify each strategy reports the correct scheme name."""

    def test_climarad_scheme_name(self) -> None:
        assert ClimaRadStrategy().scheme == "climarad"

    def test_orcon_scheme_name(self) -> None:
        assert OrconStrategy().scheme == "orcon"

    def test_itho_scheme_name(self) -> None:
        assert IthoStrategy().scheme == "itho"

    def test_nuaire_scheme_name(self) -> None:
        assert NuaireStrategy().scheme == "nuaire"

    def test_vasco_scheme_name(self) -> None:
        assert VascoStrategy().scheme == "vasco"


class TestBestHvacStrategy:
    """Verify HVAC strategy selection."""

    @pytest.mark.parametrize(
        ("scheme", "strategy_type"),
        [
            ("climarad", ClimaRadStrategy),
            ("itho", IthoStrategy),
            ("nuaire", NuaireStrategy),
            ("orcon", OrconStrategy),
            ("vasco", VascoStrategy),
        ],
    )
    def test_explicit_scheme(
        self, scheme: str, strategy_type: type[HvacStrategy]
    ) -> None:
        strategy = best_hvac_strategy("32:123456", scheme=scheme)

        assert isinstance(strategy, strategy_type)

    def test_default_is_orcon(self) -> None:
        strategy = best_hvac_strategy("32:123456")

        assert isinstance(strategy, OrconStrategy)

    def test_unknown_scheme_falls_back_to_orcon(self) -> None:
        strategy = best_hvac_strategy("32:123456", scheme="unknown")

        assert isinstance(strategy, OrconStrategy)

    def test_orcon_hrc350_model_selects_first_domain_capability(self) -> None:
        strategy = best_hvac_strategy(
            "32:134044", scheme="orcon", model="VMD-15RMS64"
        )

        assert isinstance(strategy, OrconHrc350Strategy)

    def test_other_orcon_model_keeps_second_domain_capability(self) -> None:
        strategy = best_hvac_strategy(
            "32:153289", scheme="orcon", model="VMD-15RMS86-2"
        )

        assert type(strategy) is OrconStrategy


class TestQuirkDispatch:
    """Verify scheme-specific quirk dispatch."""

    def test_climarad_applies_ventura_12a0_mapping(self) -> None:
        payload = {
            "hvac_index": "01",
            SZ_REL_HUMIDITY: 0.45,
            "temperature": 18.5,
        }

        result = apply_hvac_quirks(
            payload, None, Code._12A0, strategy=ClimaRadStrategy()
        )

        assert SZ_REL_HUMIDITY not in result
        assert result["supply_temp"] == 18.5

    def test_orcon_does_not_apply_climarad_12a0_mapping(self) -> None:
        payload = {"hvac_index": "01", "temperature": 18.5}

        result = apply_hvac_quirks(
            payload, None, Code._12A0, strategy=OrconStrategy()
        )

        assert "supply_temp" not in result
        assert result["temperature"] == 18.5

    def test_no_strategy_preserves_legacy_behavior(self) -> None:
        payload = {"hvac_index": "01", "temperature": 18.5}

        result = apply_hvac_quirks(payload, None, Code._12A0)

        assert result["supply_temp"] == 18.5
