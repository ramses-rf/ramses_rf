# DIS Parameter Command Support

## Summary
This PR adds support for fan parameter commands (get/set_fan_param) to HvacDisplayRemote devices. Since DIS devices inherit from HvacRemote, they can already perform all REM commands, making this implementation straightforward.

## Changes

1. Add get_fan_param and set_fan_param methods to HvacDisplayRemote:
```python
class HvacDisplayRemote(HvacRemote):  # DIS
    """The DIS (display switch)."""

    _SLUG: str = DevType.DIS

    async def get_fan_param(self, fan_id: str, param_id: str) -> Any:
        """Get a fan parameter through this DIS device."""
        cmd = Command.get_fan_param(
            fan_id=fan_id,
            param_id=param_id,
            src_id=self.id
        )
        return await self._gwy.send_cmd(cmd)

    async def set_fan_param(self, fan_id: str, param_id: str, value: Any) -> None:
        """Set a fan parameter through this DIS device."""
        cmd = Command.set_fan_param(
            fan_id=fan_id,
            param_id=param_id,
            value=value,
            src_id=self.id
        )
        await self._gwy.send_cmd(cmd)
```

2. Update the device documentation to reflect DIS's ability to handle parameter commands.

## Benefits

1. Provides a clean, object-oriented interface for parameter commands
2. Maintains consistency with existing REM command patterns
3. Simplifies implementation in ramses_cc by providing direct methods
4. Properly encapsulates DIS-FAN relationships

## Testing

1. Unit tests for parameter command construction
2. Integration tests with DIS device
3. Type checking and error handling

## Usage Example

```python
# In ramses_cc:
class RamsesFanParameterEntity(RamsesEntity):
    def __init__(self, broker, fan_device, dis_device, parameter_id):
        super().__init__(broker, fan_device, entity_description)
        self._dis_device = dis_device
        self._parameter_id = parameter_id
        
    async def async_update(self):
        value = await self._dis_device.get_fan_param(
            fan_id=self._device.id,
            param_id=self._parameter_id
        )
        self._attr_native_value = value
        
    async def async_set_value(self, value):
        await self._dis_device.set_fan_param(
            fan_id=self._device.id,
            param_id=self._parameter_id,
            value=value
        )
```
