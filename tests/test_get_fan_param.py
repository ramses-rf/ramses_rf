#!/usr/bin/env python3
# pyright: reportUnusedImport=false
"""Simple test script to get a fan parameter from a Ramses device."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable
from typing import Any, TypeVar

import pytest

from ramses_rf import Gateway
from ramses_tx.command import Command
from ramses_tx.exceptions import CommandInvalid
from ramses_tx.message import Message

# Type aliases
DeviceIdT = str
DeviceTraitsT = dict[str, Any]

# Message handler type that can be either sync or async
MsgHandlerT = Callable[[Message], Any]  # Can return None or Coroutine

# Type definitions
_T = TypeVar("_T")

# Configuration
MQTT_URL = "mqtt://esp1:j%40diebla@192.168.0.84:1883"
HGI_ID: str = "18:149488"
SOURCE_DEVICE_ID: str = "37:168270"  # DIS device
FAN_DEVICE_ID: str = "32:153289"  # FAN device
# Use a parameter ID that exists in _2411_PARAMS_SCHEMA (from ramses_tx.ramses)
# 31 = Time to change filter (days)
PARAMETER_ID = "31"
REQUEST_TIMEOUT = 10  # Seconds to wait for a response

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_LOGGER = logging.getLogger(__name__)

# Known devices - include the HGI device
KNOWN_DEVICES: dict[str, dict[str, Any]] = {
    HGI_ID: {"class": "HGI"},  # Gateway
    SOURCE_DEVICE_ID: {"class": "DIS", "faked": True},
    FAN_DEVICE_ID: {"class": "FAN"},
}


class FanParamTest:
    """Test class for fan parameter functionality.

    This class provides methods to test the fan parameter functionality
    by sending commands to a Ramses RF gateway and verifying the responses.
    """

    def __init__(self) -> None:
        """Initialize the test class with default values.

        Attributes:
            response: Stores the last response received from the gateway
            response_event: Event to signal when a response is received
            gwy: The Ramses RF gateway instance
            loop: The asyncio event loop
            task: Background task for running the test
        """
        self.gwy: Gateway | None = None
        self.response_event: asyncio.Event = asyncio.Event()
        self.response: dict[str, Any] | None = None

    async def setup(self) -> None:
        """Set up the test environment.

        This method initializes the gateway, sets up message handlers,
        and prepares the test environment for sending commands.

        Raises:
            RuntimeError: If there's an error during setup
        """
        _LOGGER.info("Starting test setup...")

        try:
            # Create a gateway instance with type-ignored known_list
            # since we've relaxed the type to Dict[str, Any]
            self.gwy = Gateway(
                port_name=MQTT_URL,
                known_list=KNOWN_DEVICES,  # type: ignore[arg-type]
                config={"enforce_known_list": True},
                loop=asyncio.get_event_loop(),
            )

            # Start the gateway
            await self.gwy.start()

            _LOGGER.debug("Registering message handler with gateway")

            # Register our synchronous message handler with the gateway
            # The handler will be called for all messages, we filter in handle_message()
            # Create a synchronous wrapper around the async handler
            def sync_handler(msg: Message) -> None:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Schedule the coroutine to run in the background
                    asyncio.create_task(self.handle_message_async(msg))
                else:
                    # If no event loop is running, run the coroutine directly
                    loop.run_until_complete(self.handle_message_async(msg))

            # Register the message handler - ignore the None return value
            self.gwy.add_msg_handler(sync_handler)
            _LOGGER.debug("Registered message handler with gateway")
            _LOGGER.info("Test setup complete")

        except Exception as err:
            _LOGGER.error("Error during setup: %s", err, exc_info=True)
            await self.cleanup()
            raise

    async def cleanup(self) -> None:
        """Clean up resources.

        This method stops the gateway, cancels any running tasks,
        and performs necessary cleanup to ensure a clean test environment.
        """
        _LOGGER.info("Cleaning up test resources...")

        # Clean up the gateway if it exists
        if hasattr(self, "gwy") and self.gwy is not None:
            _LOGGER.debug("Stopping gateway...")
            try:
                await self.gwy.stop()
            except (asyncio.CancelledError, RuntimeError) as err:
                _LOGGER.warning("Gateway stop interrupted: %s", err)
                raise
            except (ConnectionError, OSError, asyncio.InvalidStateError) as err:
                _LOGGER.warning("Error during gateway cleanup: %s", err)
            except (AttributeError, TypeError) as err:
                _LOGGER.warning("Error accessing gateway stop method: %s", err)
            except Exception as err:  # noqa: BLE001
                # This is intentionally broad as a last resort to ensure cleanup completes
                # even in the face of unexpected errors. This is safe because:
                # 1. We're in a cleanup method where the primary goal is to release resources
                # 2. We log the full error with traceback for debugging
                # 3. We re-raise only critical errors that shouldn't be suppressed
                _LOGGER.warning(
                    "Unexpected error during gateway cleanup (continuing anyway): %s",
                    err,
                    exc_info=True,
                )
                # Re-raise only critical errors that should not be ignored
                if isinstance(err, MemoryError | SystemError | KeyboardInterrupt):
                    raise

        _LOGGER.info("Cleanup complete")

    async def handle_message_async(self, msg: Message) -> None:
        """Handle incoming messages asynchronously.

        Args:
            msg: The incoming message to process

        This method processes incoming messages from the gateway and updates
        the test state accordingly when a matching response is received.
        """
        try:
            # Skip if we've already processed this message
            if not hasattr(msg, "code") or msg.code != "2411":
                return

            # Log the message details
            _LOGGER.info("Received message: %s", msg)
            _LOGGER.info(
                "Message details: code=%s, payload=%s, src=%s, dst=%s",
                msg.code,
                msg.payload,
                getattr(msg.src, "id", "unknown"),
                getattr(msg.dst, "id", "unknown"),
            )

            # Check if this is a response to our request
            is_response = (
                msg.code == "2411"
                and getattr(msg.src, "id", None) == FAN_DEVICE_ID
                and getattr(msg.dst, "id", None) == SOURCE_DEVICE_ID
            )

            if is_response:
                _LOGGER.info("Received response from fan: %s", msg.payload)

                # Store the response and set the event
                self.response = {
                    "code": msg.code,
                    "payload": msg.payload,
                    "src": getattr(msg.src, "id", "unknown"),
                    "dst": getattr(msg.dst, "id", "unknown"),
                }
                self.response_event.set()

        except (AttributeError, ValueError) as err:
            _LOGGER.error("Error processing message attributes: %s", str(err))
            self.response = {"error": f"Message processing error: {str(err)}"}
            self.response_event.set()

    async def _check_gateway_availability(self) -> dict[str, str] | None:
        """Check if gateway and protocol are available.

        Returns:
            Error dict if not available, None otherwise
        """
        if self.gwy is None:
            error_msg = "Gateway not available"
            _LOGGER.error(error_msg)
            return {"error": error_msg}

        protocol = getattr(self.gwy, "_protocol", None)
        if protocol is None:  # pylint: disable=protected-access
            error_msg = "Protocol not available"
            _LOGGER.error(error_msg)
            return {"error": error_msg}

        return None

    async def _send_fan_param_command(
        self, fan_id: str, param_id: str
    ) -> dict[str, Any] | None:
        """Send a fan parameter command and wait for response.

        Args:
            fan_id: The ID of the fan device
            param_id: The parameter ID to read

        Returns:
            Response dict or None if there was an error
        """
        try:
            _LOGGER.info(
                "Creating command with params: fan_id=%s, " "param_id=%s, src_id=%s",
                fan_id,
                param_id,
                SOURCE_DEVICE_ID,
            )

            cmd = Command.get_fan_param(
                fan_id=fan_id, param_id=param_id, src_id=SOURCE_DEVICE_ID
            )
            _LOGGER.info("Command created: %s", cmd)

            # Log command attributes for debugging
            cmd_attrs = [a for a in dir(cmd) if not a.startswith("_")]
            _LOGGER.debug("Command attributes: %s", cmd_attrs)

            # Log protocol details for debugging
            protocol = getattr(self.gwy, "_protocol", None)
            if protocol is not None:
                _LOGGER.info("Protocol details:")
                _LOGGER.info("  Protocol class: %s", protocol.__class__.__name__)
                protocol_attrs = [a for a in dir(protocol) if not a.startswith("_")]
                _LOGGER.info("  Protocol attributes: %s", protocol_attrs)

            # Send the command
            _LOGGER.info("Sending command...")
            protocol = getattr(self.gwy, "_protocol", None)
            if protocol is None:
                error_msg = "Cannot send command: protocol not available"
                _LOGGER.error(error_msg)
                return {"error": error_msg}

            await protocol.send_cmd(cmd)
            _LOGGER.info("Command sent successfully")
            return None

        except (AttributeError, ValueError) as e:
            error_msg = f"Error creating/sending command: {str(e)}"
            _LOGGER.error(error_msg, exc_info=True)
            return {"error": error_msg}

    async def _wait_for_fan_response(self) -> dict[str, Any]:
        """Wait for and process the fan parameter response.

        Returns:
            Response dict with the result or error
        """
        _LOGGER.info("Waiting for response (timeout: 10s)...")
        try:
            await asyncio.wait_for(self.response_event.wait(), timeout=10.0)

            if self.response is None:
                return {"error": "No response received (response is None)"}

            _LOGGER.info("Response received: %s", self.response)
            return dict(self.response)

        except TimeoutError:
            error_msg = "Timeout waiting for response (10s elapsed)"
            _LOGGER.error(error_msg)
            return {"error": error_msg}

    async def get_fan_parameter(self, fan_id: str, param_id: str) -> dict[str, Any]:
        """Get a fan parameter value.

        Args:
            fan_id: The ID of the fan device
            param_id: The parameter ID to read

        Returns:
            A dictionary containing the response data or error information
        """
        _LOGGER.info("Getting fan parameter: fan_id=%s, param_id=%s", fan_id, param_id)

        # Check gateway and protocol availability
        error_response = await self._check_gateway_availability()
        if error_response:
            return error_response

        # Reset the response event and clear any previous response
        self.response_event.clear()
        self.response = None

        try:
            # Send the command
            error = await self._send_fan_param_command(fan_id, param_id)
            if error:
                return error

            # Wait for and process the response
            return await self._wait_for_fan_response()

        except (TimeoutError, ConnectionError) as err:
            error_msg = f"Communication error: {str(err)}"
            _LOGGER.error(error_msg)
            return {"error": error_msg}
        except Exception as err:  # pylint: disable=broad-except
            error_msg = f"Unexpected error: {str(err)}"
            _LOGGER.error(error_msg, exc_info=True)
            return {"error": error_msg}

        finally:
            # Clean up
            self.response_event.clear()
            self.response = None


def setup_logging() -> tuple[logging.Handler, logging.Logger]:
    """Set up logging configuration.

    Returns:
        tuple: A tuple containing the console handler and root logger.
    """
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s", datefmt="%H:%M:%S"
    )
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(console_handler)

    return console_handler, root_logger


def log_test_configuration() -> None:
    """Log the test configuration."""
    _LOGGER.info("=== STARTING FAN PARAMETER TEST ===")
    _LOGGER.info("Configuration:")
    _LOGGER.info("  MQTT URL: %s", MQTT_URL)
    _LOGGER.info("  HGI ID: %s", HGI_ID)
    _LOGGER.info("  SOURCE DEVICE: %s", SOURCE_DEVICE_ID)
    _LOGGER.info("  FAN DEVICE: %s", FAN_DEVICE_ID)
    _LOGGER.info("  PARAMETER ID: %s", PARAMETER_ID)


async def log_connected_devices(test: FanParamTest) -> None:
    """Log information about connected devices.

    Args:
        test: The test instance containing the gateway.
    """
    if (
        test.gwy is None
        or not hasattr(test.gwy, "device_by_id")
        or not test.gwy.device_by_id
    ):
        _LOGGER.warning(
            "Could not list connected devices: "
            "Gateway not available or no devices found"
        )
        return

    _LOGGER.info("\n=== CONNECTED DEVICES ===")
    for dev_id, device in test.gwy.device_by_id.items():
        _LOGGER.info("  - %s: %s", dev_id, device.__class__.__name__)
        if dev_id == FAN_DEVICE_ID:
            _log_device_attributes(device)


def _log_device_attributes(device: Any) -> None:
    """Log attributes of a device.

    Args:
        device: The device to log attributes for.
    """
    _LOGGER.info("    FAN device attributes:")
    for attr in dir(device):
        if not attr.startswith("_"):
            try:
                value = getattr(device, attr)
                if not callable(value):
                    _LOGGER.info("      %s: %s", attr, value)
            except (AttributeError, TypeError) as err:
                _LOGGER.debug("      Could not access %s: %s", attr, err)


def process_test_results(response: dict[str, Any] | None) -> int:
    """Process and log test results.

    Args:
        response: The response from the fan parameter request.

    Returns:
        int: 0 for success, 1 for failure.
    """
    if response and "error" not in response:
        _log_successful_response(response)
        return 0

    _log_failed_response(response)
    return 1


def _log_successful_response(response: dict[str, Any]) -> None:
    """Log details of a successful response.

    Args:
        response: The successful response dictionary.
    """
    _LOGGER.info(
        "SUCCESS: Retrieved parameter %s = %s",
        response.get("parameter", "unknown"),
        response.get("value", "unknown"),
    )

    if "payload" in response and isinstance(response["payload"], dict):
        _LOGGER.info("Payload details:")
        for k, v in sorted(response["payload"].items()):
            _LOGGER.info("  %s: %s", k, v)


def _log_failed_response(response: Any) -> None:
    """Log details of a failed response.

    Args:
        response: The failed response or None.
    """
    error_msg = (
        response.get("error", "Unknown error")
        if isinstance(response, dict)
        else "No response received"
    )
    _LOGGER.error("FAILED: %s", error_msg)


async def cleanup_test(
    test: FanParamTest | None,
    console_handler: logging.Handler,
    root_logger: logging.Logger,
) -> None:
    """Clean up test resources.

    Args:
        test: The test instance to clean up.
        console_handler: The console handler to remove.
        root_logger: The root logger to modify.
    """
    _LOGGER.info("\n=== CLEANING UP ===")
    try:
        if test is not None:
            await test.cleanup()
        _LOGGER.info("Cleanup complete")
    except (asyncio.CancelledError, RuntimeError) as err:
        _LOGGER.warning("Cleanup interrupted: %s", str(err))
        raise
    except TimeoutError as err:
        _LOGGER.warning("Cleanup timed out or connection error: %s", str(err))
    except (AttributeError, TypeError, ValueError) as err:
        _LOGGER.warning("Error accessing test cleanup attributes: %s", str(err))
    except Exception as cleanup_error:  # noqa: BLE001
        # This is intentionally broad as a final fallback to ensure cleanup completes
        # even in the face of unexpected errors. This is safe because:
        # 1. We're in a test cleanup phase where the primary goal is to release resources
        # 2. We log the full error with traceback for debugging
        # 3. We re-raise only critical errors that shouldn't be suppressed
        _LOGGER.error(
            "Unexpected error during test cleanup (continuing anyway): %s",
            cleanup_error,
            exc_info=True,
        )
        # Re-raise only critical errors that should not be ignored
        if isinstance(cleanup_error, MemoryError | SystemError | KeyboardInterrupt):
            raise
    finally:
        root_logger.removeHandler(console_handler)
        console_handler.close()


async def main() -> int:
    """Main test function.

    Returns:
        int: 0 if the test was successful, non-zero otherwise.
    """
    # Set up logging
    console_handler, root_logger = setup_logging()
    log_test_configuration()

    test: FanParamTest | None = None
    exit_code = 1  # Default to error

    try:
        # Setup the test environment
        _LOGGER.info("Setting up test environment...")
        test = FanParamTest()
        await test.setup()
        _LOGGER.info("Test environment setup complete")

        # Log connected devices
        await log_connected_devices(test)

        # Get the fan parameter
        _LOGGER.info("\n=== INITIATING FAN PARAMETER REQUEST ===")
        response = await test.get_fan_parameter(FAN_DEVICE_ID, PARAMETER_ID)

        # Process and log results
        _LOGGER.info("\n=== TEST RESULTS ===")
        exit_code = process_test_results(response)

    except TimeoutError as err:
        _LOGGER.error("Test timed out: %s", str(err))
        exit_code = 1
    except asyncio.CancelledError as err:
        _LOGGER.error("Test execution was cancelled: %s", str(err))
        exit_code = 1
    except (ConnectionError, OSError) as err:
        _LOGGER.error("Network or I/O error during test: %s", str(err))
        exit_code = 1
    except (ValueError, KeyError, AttributeError) as err:
        _LOGGER.error("Test configuration error: %s", str(err))
        exit_code = 1
    except RuntimeError as err:
        _LOGGER.error("Runtime error during test: %s", str(err))
        exit_code = 1
    except Exception as err:  # noqa: BLE001
        # This is intentionally broad as a final fallback to ensure we don't crash
        # with a traceback for unexpected errors. This is safe because:
        # 1. We're in a test environment where we want to capture all errors
        # 2. We log the full exception details for debugging
        # 3. We still re-raise critical errors that shouldn't be caught
        _LOGGER.exception(
            "Unexpected error during test execution (test will fail): %s", str(err)
        )
        # Only exit with error code if it's a critical error
        if isinstance(err, MemoryError | SystemError | KeyboardInterrupt):
            _LOGGER.critical("Critical error detected, re-raising")
            raise
        exit_code = 1
    finally:
        await cleanup_test(test, console_handler, root_logger)
        _LOGGER.info(
            "=== TEST COMPLETED WITH %s ===", "SUCCESS" if exit_code == 0 else "FAILURE"
        )

    return exit_code


class TestFanParamValidation:
    """Test validation of fan parameter IDs."""

    def test_valid_parameter_ids(self) -> None:
        """Test that valid parameter IDs are accepted."""
        # Test valid parameter IDs
        valid_ids = ["00", "FF", "0A", "a1", "B2"]
        for param_id in valid_ids:
            # Should not raise
            cmd = Command.get_fan_param("12:345678", param_id, src_id="22:222222")
            assert cmd is not None
            assert cmd.code == "2411"
            assert cmd.verb == "RQ"
            assert cmd.dst.id == "12:345678"
            assert cmd.payload == f"0000{param_id.upper()}"

    @pytest.mark.parametrize(
        "invalid_id",
        [
            ("",),  # empty string
            ("1",),  # too short
            ("123",),  # too long
            ("GH",),  # invalid hex
            (" 12",),  # leading whitespace
            ("12 ",),  # trailing whitespace
            ("0x12",),  # hex prefix
            ("1G",),  # invalid hex
            ("-1",),  # negative
            ("1.0",),  # decimal
        ],
    )
    def test_invalid_parameter_ids(self, invalid_id: str) -> None:
        """Test that invalid parameter IDs raise CommandInvalid."""
        with pytest.raises(CommandInvalid):
            Command.get_fan_param("12:345678", invalid_id, src_id="22:222222")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
