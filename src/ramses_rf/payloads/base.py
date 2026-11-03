"""RAMSES RF - Dataclass Payload Layer base interface.

This module defines the abstract base class and protocol contract
for all RAMSES packet payload dataclasses.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import ItemsView, KeysView, ValuesView
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self

from .adapters import payload_to_dict

if TYPE_CHECKING:
    from ramses_rf.typing import PayloadT


@dataclass(frozen=True, slots=True)
class PayloadBase(ABC):
    """Abstract base class for RAMSES packet payload dataclasses.

    All payload classes must inherit from this class, specify slots
    and frozen, and implement binary decoding (from_bytes) and
    encoding (to_bytes) methods.
    """

    @classmethod
    @abstractmethod
    def from_bytes(
        cls, raw_data: bytes
    ) -> Self | list[Self] | PayloadBase | list[PayloadBase]:
        """Unpack raw binary payload bytes into typed dataclass instance(s).

        :param raw_data: Raw byte string representation of payload.
        :type raw_data: bytes
        :returns: A typed payload dataclass instance or list of instances.
        :rtype: Self | list[Self] | PayloadBase | list[PayloadBase]
        """
        ...

    @abstractmethod
    def to_bytes(self) -> bytes:
        """Pack payload attributes into a raw byte string for transmission.

        :returns: Packed raw byte string representation of the payload.
        :rtype: bytes
        """
        ...

    def to_dict(  # type: ignore[explicit-any]  # compat shim: subclasses vary
        self, *args: Any, **kwargs: Any
    ) -> PayloadT:
        """Convert payload dataclass to pre-0.60 dictionary format.

        :param args: Optional positional arguments for compatibility.
        :param kwargs: Optional keyword arguments for compatibility.
        :returns: Dictionary representation of the payload.
        :rtype: PayloadT
        """
        return payload_to_dict(self)

    def __getitem__(self, key: str) -> Any:  # type: ignore[explicit-any]  # dict-compat shim
        """Access payload field via dictionary lookup for pre-0.60 compatibility.

        :param key: Field name key.
        :type key: str
        :returns: Decoded field value.
        :rtype: Any
        :raises KeyError: If key is not in dictionary.
        """
        result_dict = self.to_dict()
        if isinstance(result_dict, dict):
            return result_dict[key]
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        """Check if key exists in payload dictionary.

        :param key: Field name key.
        :type key: object
        :returns: True if key exists, False otherwise.
        :rtype: bool
        """
        result_dict = self.to_dict()
        return key in result_dict if isinstance(result_dict, dict) else False

    def get(  # type: ignore[explicit-any]  # dict-compat shim
        self, key: str, default: Any = None
    ) -> Any:
        """Get value for key with optional fallback default.

        :param key: Field name key.
        :type key: str
        :param default: Fallback default value.
        :type default: Any
        :returns: Field value or default.
        :rtype: Any
        """
        result_dict = self.to_dict()
        return (
            result_dict.get(key, default)
            if isinstance(result_dict, dict)
            else default
        )

    def keys(self) -> KeysView[str]:
        """Return dictionary keys view for pre-0.60 compatibility.

        :returns: Keys view.
        :rtype: KeysView[str]
        """
        result_dict = self.to_dict()
        return (
            result_dict.keys() if isinstance(result_dict, dict) else {}.keys()
        )

    def values(self) -> ValuesView[object]:
        """Return dictionary values view for pre-0.60 compatibility.

        :returns: Values view.
        :rtype: ValuesView[Any]
        """
        result_dict = self.to_dict()
        return (
            result_dict.values()
            if isinstance(result_dict, dict)
            else {}.values()
        )

    def items(self) -> ItemsView[str, object]:
        """Return dictionary items view for pre-0.60 compatibility.

        :returns: Items view.
        :rtype: ItemsView[str, Any]
        """
        result_dict = self.to_dict()
        return (
            result_dict.items()
            if isinstance(result_dict, dict)
            else {}.items()
        )

    def hex(self) -> str:
        """Return uppercase ASCII hex representation of binary payload.

        :returns: Uppercase hex string payload representation.
        :rtype: str
        """
        return self.to_bytes().hex().upper()


def parse_index(index: int | str) -> int:
    """Parse integer or hex string zone/domain index into a byte integer.

    :param index: Zone/domain index integer or hex string
        (e.g. 1, "01", "HW", "FA").
    :type index: int | str
    :returns: Index as an unsigned 8-bit integer.
    :rtype: int
    :raises ValueError: If index is an invalid index value.
    """
    if isinstance(index, int):
        if not (0 <= index <= 255):
            raise ValueError(f"Invalid zone index: {index}")
        return index
    if index == "HW":
        return 0xFA
    result = int(index, 16)
    if not (0 <= result <= 255):
        raise ValueError(f"Invalid zone index: {index}")
    return result


parse_index = parse_index
