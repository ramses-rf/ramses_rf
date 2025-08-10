# Callback Mechanism in Ramses RF

## Overview

The callback mechanism in Ramses RF provides a way to be notified when specific message types are received from HVAC devices. This is particularly useful for handling device initialization and feature detection. The mechanism helps solve race conditions between entity creation and message reception, allowing for on-demand entity creation.

## Key Message Types

### 2411 Messages
- Used for fan parameter configuration
- Each parameter has its own RP and I codes
- Support is determined by device capabilities

### 31DA Messages
- Used for ventilation status and features
- Contains multiple parameters in a single message
- Includes features like post-heat support
- Support is determined by device model and firmware

## Key Components

### `_found_codes` Set
- Tracks which message codes have been received from the device
- Used to determine feature support (e.g., 2411 parameters, 31DA features)

### `_pending_callbacks` List
- Stores tuples of `(code, callback)`
- Callbacks are executed when their corresponding message code is received

### `add_initialization_callback` Method
```python
def add_initialization_callback(self, callback, code=None):
    """Register a callback to be called when a specific message code is received.
    
    Args:
        callback: Function to call when the message is received
        code: Message code (e.g., Code._2411, Code._31DA) or None for any message
    """
```

## Usage Examples

### Basic Usage
```python
def my_callback():
    print("Device is ready!")

# Call when any message is received
device.add_initialization_callback(my_callback)

# Or for specific message types
device.add_initialization_callback(my_callback, code=Code._31DA)
```

### Feature Detection
```python
def setup_post_heat():
    if device.post_heat_supported:
        # Initialize post-heat related entities
        pass

# Register for 31DA message callbacks
device.add_initialization_callback(setup_post_heat, code=Code._31DA)
```

## Common Message Codes

| Code   | Description                     |
|--------|---------------------------------|
| `_2411`| Fan parameters                  |
| `_31DA`| Ventilation status (post-heat)  |
| `_30C9`| Device information              |

## Best Practices

1. **Use Specific Codes** when possible to avoid unnecessary callback invocations
2. **Check Feature Flags** in callbacks to handle partial initialization
3. **Keep Callbacks Light** - move heavy processing to separate tasks
4. **Handle Errors** - callbacks should handle cases where expected data isn't available

## Implementation Details

Callbacks are processed in `_process_pending_callbacks()`, which is called from `_handle_msg()` when a message is received. The method:

1. Adds the message code to `_found_codes`
2. Executes any callbacks registered for that specific code
3. Executes any callbacks registered for all codes (when `code=None`)

This mechanism ensures that components can react to device capabilities as they're discovered, without requiring polling or complex state tracking.

### 31DA Message Handling

When a 31DA message is received, the callback mechanism will execute any registered callbacks for this specific message code. The 31DA message contains multiple parameters, including features like post-heat support. The callback can check the `post_heat_supported` feature flag to handle partial initialization.

### `add_initialization_callback` Method for 31DA Messages

```python
def add_initialization_callback(self, callback, code=Code._31DA):
    """Register a callback to be called when a 31DA message is received.
    
    Args:
        callback: Function to call when the message is received
    """
```

This method allows registering callbacks specifically for 31DA messages, making it easier to handle ventilation status and features.
