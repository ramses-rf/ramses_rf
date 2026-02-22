"""RAMSES RF - Abstract Base Classes and Interfaces."""

from typing import TYPE_CHECKING, Any, Protocol

from ramses_tx import Code, Command, Message, Packet, Priority

from .typing import DeviceIdT

if TYPE_CHECKING:
    from .entity_base import Parent


class MessageIndexInterface(Protocol):
    """Interface for the SQLite Message Index database."""

    def add(self, msg: Message) -> Message | None:
        """Add a message to the index."""
        ...

    def add_record(
        self, src: str, code: str = "", verb: str = "", payload: str = "00"
    ) -> None:
        """Add a single record to the index."""
        ...

    def rem(
        self, msg: Message | None = None, **kwargs: Any
    ) -> tuple[Message, ...] | None:
        """Remove a set of message(s) from the index."""
        ...

    def get(self, msg: Message | None = None, **kwargs: Any) -> tuple[Message, ...]:
        """Get a set of message(s) from the index."""
        ...

    def contains(self, **kwargs: Any) -> bool:
        """Check if the index contains at least 1 record matching the fields."""
        ...

    def qry(self, sql: str, parameters: tuple[str, ...]) -> tuple[Message, ...]:
        """Execute a custom SQL query returning messages."""
        ...

    def qry_field(self, sql: str, parameters: tuple[str, ...]) -> list[tuple[Any, ...]]:
        """Execute a custom SQL query returning raw fields."""
        ...

    def get_rp_codes(self, parameters: tuple[str, ...]) -> list[Code]:
        """Get a list of Codes from the index."""
        ...

    def all(self, include_expired: bool = False) -> tuple[Message, ...]:
        """Get all messages from the index."""
        ...

    def flush(self) -> None:
        """Flush the storage worker queue."""
        ...

    def stop(self) -> None:
        """Stop the database and close connections."""
        ...


class DeviceInterface(Protocol):
    """Interface for a standard RF Device."""

    @property
    def id(self) -> DeviceIdT:
        """Return the device ID.

        :return: The Device ID.
        """
        ...

    @property
    def traits(self) -> dict[str, Any]:
        """Return the device traits.

        :return: A dictionary of device traits.
        """
        ...

    def _handle_msg(self, msg: Message) -> None:
        """Process an incoming message.

        :param msg: The message to process.
        """
        ...


class GatewayInterface(Protocol):
    """Interface for the core Gateway orchestrator."""

    @property
    def msg_db(self) -> MessageIndexInterface | None:
        """Return the message database if configured."""
        ...

    @msg_db.setter
    def msg_db(self, value: MessageIndexInterface | None) -> None:
        """Set the message database."""
        ...

    @property
    def config(self) -> Any:
        """Return the gateway configuration."""
        ...

    def get_device(
        self,
        device_id: DeviceIdT,
        *,
        msg: Message | None = None,
        parent: "Parent | None" = None,
        child_id: str | None = None,
        is_sensor: bool | None = None,
    ) -> DeviceInterface:
        """Retrieve or create a device."""
        ...

    async def async_send_cmd(
        self,
        cmd: Command,
        /,
        *,
        priority: Priority = Priority.DEFAULT,
        wait_for_reply: bool | None = True,
        max_retries: int = 3,
        timeout: float = 3.0,
    ) -> Packet:
        """Send a command asynchronously and return the resulting packet."""
        ...
