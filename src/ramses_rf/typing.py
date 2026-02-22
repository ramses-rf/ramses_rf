"""RAMSES RF - Domain-specific type definitions."""

from typing import Any, TypeAlias

from ramses_tx.typing import DeviceIdT as TxDeviceIdT, DevIndexT as TxIndexT

DeviceIdT: TypeAlias = TxDeviceIdT
IndexT: TypeAlias = TxIndexT

DeviceTraitsT: TypeAlias = dict[str, Any]
DeviceListT: TypeAlias = dict[DeviceIdT, DeviceTraitsT]
