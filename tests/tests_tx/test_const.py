"""Test suite for ramses_tx.const module, focusing on AttrDict stability."""

import pytest

from ramses_tx.const import AttrDict


class TestAttrDict:
    """Tests for the AttrDict utility class."""

    def test_attr_dict_lookup_happy_path(self) -> None:
        """Test that AttrDict resolves attributes from the lookup tables."""
        # Setup: Pass data directly into the tables via __init__
        # Structure: main_table={slug: {code: ...}}, attr_table={name: value}
        mock_main = {"DHW": {"0D": "Hot Water"}}
        mock_attr = {"custom_key": "custom_value"}

        attr_dict = AttrDict(mock_main, mock_attr)

        # Verify dot notation lookup works via __getattr__ logic
        assert attr_dict.custom_key == "custom_value"
        # Verify main_table lookup logic (based on your __getattr__ code: list(keys)[0])
        assert attr_dict.DHW == "0D"

    def test_getattr_recursion_fix(self) -> None:
        """Test that accessing a missing attribute raises AttributeError, not RecursionError.

        This verifies the fix for TX-CONST-01.
        """
        attr_dict = AttrDict({}, {})

        # This should raise AttributeError immediately.
        # If the bug exists, this would crash with RecursionError.
        with pytest.raises(
            AttributeError, match=r"'AttrDict' object has no attribute 'missing'"
        ):
            _ = attr_dict.missing

    def test_getattr_raises_correct_exception_type(self) -> None:
        """Ensure standard AttributeError is raised, satisfying protocol expectations."""
        attr_dict = AttrDict({}, {})

        try:
            _ = attr_dict.non_existent_key
        except AttributeError:
            pass  # This is the expected behavior
        except RecursionError:
            pytest.fail(
                "AttrDict raised RecursionError (Stack Overflow) instead of AttributeError"
            )
