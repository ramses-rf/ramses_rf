"""RAMSES RF - OpenTherm Bridge Device."""

from __future__ import annotations

import logging
from datetime import timedelta as td
from typing import Any, Final

from ramses_rf.const import (
    HEARTBEAT_TIMEOUT_OTB,
    SZ_AIR_PRESSURE_FAULT,
    SZ_BOILER_EXHAUST_TEMP,
    SZ_BOILER_OUTPUT_TEMP,
    SZ_BOILER_RETURN_TEMP,
    SZ_BOILER_SETPOINT,
    SZ_BURNER_FAILED_STARTS,
    SZ_BURNER_HOURS,
    SZ_BURNER_STARTS,
    SZ_CH_ACTIVE,
    SZ_CH_ENABLED,
    SZ_CH_MAX_SETPOINT,
    SZ_CH_PUMP_HOURS,
    SZ_CH_PUMP_STARTS,
    SZ_CH_SETPOINT,
    SZ_CH_WATER_PRESSURE,
    SZ_COOLING_ACTIVE,
    SZ_COOLING_ENABLED,
    SZ_DHW_ACTIVE,
    SZ_DHW_BLOCKING,
    SZ_DHW_BURNER_HOURS,
    SZ_DHW_BURNER_STARTS,
    SZ_DHW_ENABLED,
    SZ_DHW_FLOW_RATE,
    SZ_DHW_PUMP_HOURS,
    SZ_DHW_PUMP_STARTS,
    SZ_DHW_SETPOINT,
    SZ_DHW_TEMP,
    SZ_FAULT_PRESENT,
    SZ_FLAME_ACTIVE,
    SZ_FLAME_SIGNAL_LOW,
    SZ_GAS_FLAME_FAULT,
    SZ_LOCKOUT_RESET,
    SZ_LOW_WATER_PRESSURE,
    SZ_MAX_REL_MODULATION,
    SZ_OEM_CODE,
    SZ_OEM_FAULT_CODE,
    SZ_OPENTHERM_PARAMS,
    SZ_OPENTHERM_SCHEMA,
    SZ_OPENTHERM_STATE,
    SZ_OTC_ACTIVE,
    SZ_OUTSIDE_TEMP,
    SZ_RAMSES_II_PARAMS,
    SZ_RAMSES_II_SCHEMA,
    SZ_REL_MODULATION_LEVEL,
    SZ_SERVICE_REQUEST,
    SZ_SUMMER_MODE,
    SZ_WATER_OVER_TEMPERATURE,
    DevType,
)
from ramses_rf.models import (
    DeviceTraits,
    OpenThermCounters,
    OpenThermFaultFlags,
    OpenThermFlags,
    OpenThermState,
    OpenThermStateDTO,
    OpenThermTemperatures,
)
from ramses_rf.typing import SchemaT
from ramses_tx.const import FC
from ramses_tx.typing import PayDictT

from .heat_actuators import HeatDemand

#
# NOTE: All debug flags should be False for deployment to end-users
_DBG_ENABLE_DEPRECATION: Final[bool] = False
_DBG_EXTRA_OTB_DISCOVERY: Final[bool] = False

_LOGGER = logging.getLogger(__name__)


# NOTE: Telemetry is read from self.opentherm_state; use_native_ot applies to command dispatching
class OtbGateway(HeatDemand):  # OTB (10): 3220 (22D9, others)
    """The OTB class, specifically an OpenTherm Bridge (R8810A Bridge)."""

    # see: https://www.opentherm.eu/request-details/?post_ids=2944
    # see: https://www.automatedhome.co.uk/vbulletin/showthread.php?6400-(New)-cool-mode-in-Evohome

    _SLUG = DevType.OTB
    _STATE_ATTR = SZ_REL_MODULATION_LEVEL

    OPENTHERM_STATE: Final = SZ_OPENTHERM_STATE

    def __init__(  # type: ignore[explicit-any]  # heterogeneous construction/schema kwargs
        self, *args: Any, traits: DeviceTraits | None = None, **kwargs: Any
    ) -> None:
        """Initialize the OpenTherm Bridge device software twin.

        :param args: Positional arguments passed to base class.
        :param traits: Strictly typed traits definition object.
        :type traits: DeviceTraits | None
        :param kwargs: Keyword arguments passed to base class.
        """
        super().__init__(*args, traits=traits, **kwargs)
        self.opentherm_state = OpenThermState()

        self._child_id = FC  # NOTE: domain_id

    _HEARTBEAT_TIMEOUT: td = HEARTBEAT_TIMEOUT_OTB

    @property
    def flags(self) -> OpenThermFlags:
        """Return the immutable OpenTherm status flags.

        :return: The OpenThermFlags dataclass.
        :rtype: OpenThermFlags
        """
        return self.opentherm_state.flags

    @property
    def faults(self) -> OpenThermFaultFlags:
        """Return the immutable OpenTherm application fault flags.

        :return: The OpenThermFaultFlags dataclass.
        :rtype: OpenThermFaultFlags
        """
        return self.opentherm_state.faults

    @property
    def temperatures(self) -> OpenThermTemperatures:
        """Return the immutable OpenTherm temperature telemetry.

        :return: The OpenThermTemperatures dataclass.
        :rtype: OpenThermTemperatures
        """
        return self.opentherm_state.temperatures

    @property
    def counters(self) -> OpenThermCounters:
        """Return the immutable OpenTherm diagnostic runtime counters.

        :return: The OpenThermCounters dataclass.
        :rtype: OpenThermCounters
        """
        return self.opentherm_state.counters

    async def boiler_exhaust_temp(
        self,
    ) -> float | None:  # 3220|21, no known RAMSES equivalent
        """Return boiler exhaust temperature in degrees Celsius."""
        return self.opentherm_state.temperatures.boiler_exhaust

    async def boiler_output_temp(self) -> float | None:  # 3220|19, or 3200
        """Return boiler output temperature in degrees Celsius."""
        return self.opentherm_state.temperatures.boiler_output

    async def boiler_return_temp(self) -> float | None:  # 3220|1C, or 3210
        """Return boiler return temperature in degrees Celsius."""
        return self.opentherm_state.temperatures.boiler_return

    async def boiler_setpoint(self) -> float | None:  # 3220|01, or 22D9
        """Return target boiler setpoint in degrees Celsius."""
        return self.opentherm_state.temperatures.boiler_setpoint

    async def ch_max_setpoint(self) -> float | None:  # 3220|39, or 1081
        """Return maximum central heating setpoint limit."""
        return self.opentherm_state.temperatures.ch_max_setpoint

    async def ch_setpoint(self) -> float | None:  # 3EF0 (byte 7, only R8820A?)
        """Return central heating setpoint in degrees Celsius."""
        return self.opentherm_state.temperatures.ch_setpoint

    async def ch_water_pressure(self) -> float | None:  # 3220|12, or 1300
        """Return central heating water pressure in bar."""
        return self.opentherm_state.ch_water_pressure

    async def dhw_flow_rate(self) -> float | None:  # 3220|13, or 12F0
        """Return domestic hot water flow rate in litres per minute."""
        return self.opentherm_state.dhw_flow_rate

    async def dhw_setpoint(self) -> float | None:  # 3220|38, or 10A0
        """Return domestic hot water temperature setpoint."""
        return self.opentherm_state.temperatures.dhw_setpoint

    async def dhw_temp(self) -> float | None:  # 3220|1A, or 1260
        """Return domestic hot water temperature in degrees Celsius."""
        return self.opentherm_state.temperatures.dhw

    async def max_rel_modulation(
        self,
    ) -> float | None:  # 3220|0E, or 3EF0 (byte 8) NOTE: not reliable?
        """Return maximum relative modulation limit."""
        return self.opentherm_state.max_rel_modulation

    async def oem_code(
        self,
    ) -> float | None:  # 3220|73, no known RAMSES equivalent
        """Fetch the OEM diagnostic code directly from the CQRS read-model.

        :return: The OEM code if available in the state, otherwise None.
        :rtype: float | None
        """
        if self.opentherm_state and self.opentherm_state.oem_code is not None:
            return float(self.opentherm_state.oem_code)
        return None

    async def oem_fault_code(
        self,
    ) -> int | None:  # 3220|05 (LB), no known RAMSES equivalent
        """Return the OEM fault code from msg_id 0x05.

        The baseline for "no fault" is vendor-specific (0 on ATAG,
        255 on Vaillant/VR33), so the raw code is exposed as-is.
        """
        return self.opentherm_state.oem_fault_code

    async def outside_temp(self) -> float | None:  # 3220|1B, 1290
        """Return outside air temperature in degrees Celsius."""
        return self.opentherm_state.temperatures.outside

    async def rel_modulation_level(
        self,
    ) -> float | None:  # 3220|11, or 3EF0/3EF1 NOTE: not reliable?
        """Return relative modulation percentage (0.0 to 1.0)."""
        return self.opentherm_state.rel_modulation_level

    async def ch_active(
        self,
    ) -> bool | None:  # 3220|00, or 3EF0 (byte 3) NOTE: not reliable?
        """Return whether central heating is actively running."""
        return self.opentherm_state.flags.ch_active

    async def ch_enabled(
        self,
    ) -> bool | None:  # 3220|00, or 3EF0 (byte 6) NOTE: not reliable?
        """Return whether central heating is enabled."""
        return self.opentherm_state.flags.ch_enabled

    async def cooling_active(
        self,
    ) -> bool | None:  # 3220|00, TODO: no known RAMSES
        """Return whether cooling is actively running."""
        return self.opentherm_state.flags.cooling_active

    async def cooling_enabled(
        self,
    ) -> bool | None:  # 3220|00, TODO: no known RAMSES
        """Return whether cooling is enabled."""
        return self.opentherm_state.flags.cooling_enabled

    async def dhw_active(
        self,
    ) -> bool | None:  # 3220|00, or 3EF0 (byte 3) NOTE: not reliable?
        """Return whether domestic hot water heating is active."""
        return self.opentherm_state.flags.dhw_active

    async def dhw_blocking(
        self,
    ) -> bool | None:  # 3220|00, TODO: no known RAMSES
        """Return whether domestic hot water blocking is active."""
        return self.opentherm_state.flags.dhw_blocking

    async def dhw_enabled(
        self,
    ) -> bool | None:  # 3220|00, TODO: no known RAMSES
        """Return whether domestic hot water heating is enabled."""
        return self.opentherm_state.flags.dhw_enabled

    async def fault_present(
        self,
    ) -> bool | None:  # 3220|00, TODO: no known RAMSES
        """Return whether an OpenTherm fault is present."""
        return self.opentherm_state.flags.fault_present

    async def flame_active(
        self,
    ) -> bool | None:  # 3220|00, or 3EF0 (byte 3) NOTE: not reliable?
        """Return whether boiler flame is active."""
        return self.opentherm_state.flags.flame_active

    async def otc_active(
        self,
    ) -> bool | None:  # 3220|00, TODO: no known RAMSES
        """Return whether outside temperature compensation is active."""
        return self.opentherm_state.flags.otc_active

    async def summer_mode(
        self,
    ) -> bool | None:  # 3220|00, TODO: no known RAMSES
        """Return whether summer mode is active."""
        return self.opentherm_state.flags.summer_mode

    async def service_request(
        self,
    ) -> bool | None:  # 3220|05 (HB0), no known RAMSES
        """Return whether the boiler requests a service."""
        return self.opentherm_state.faults.service_request

    async def lockout_reset(
        self,
    ) -> bool | None:  # 3220|05 (HB1), no known RAMSES
        """Return whether remote lockout-reset is enabled."""
        return self.opentherm_state.faults.lockout_reset

    async def low_water_pressure(
        self,
    ) -> bool | None:  # 3220|05 (HB2), no known RAMSES
        """Return whether the boiler reports low water pressure."""
        return self.opentherm_state.faults.low_water_pressure

    async def gas_flame_fault(
        self,
    ) -> bool | None:  # 3220|05 (HB3), no known RAMSES
        """Return whether the boiler reports a gas/flame fault."""
        return self.opentherm_state.faults.gas_flame_fault

    async def air_pressure_fault(
        self,
    ) -> bool | None:  # 3220|05 (HB4), no known RAMSES
        """Return whether the boiler reports an air pressure fault."""
        return self.opentherm_state.faults.air_pressure_fault

    async def water_over_temperature(
        self,
    ) -> bool | None:  # 3220|05 (HB5), no known RAMSES
        """Return whether the boiler reports a water over-temperature."""
        return self.opentherm_state.faults.water_over_temperature

    async def opentherm_schema(self) -> SchemaT:
        """Return OpenTherm topology configuration schema."""
        return {}

    async def opentherm_counters(self) -> SchemaT:  # all are U16
        """Return OpenTherm diagnostic runtime counters."""
        return {
            SZ_BURNER_HOURS: self.opentherm_state.counters.burner_hours,
            SZ_BURNER_STARTS: self.opentherm_state.counters.burner_starts,
            SZ_BURNER_FAILED_STARTS: self.opentherm_state.counters.burner_failed_starts,
            SZ_CH_PUMP_HOURS: self.opentherm_state.counters.ch_pump_hours,
            SZ_CH_PUMP_STARTS: self.opentherm_state.counters.ch_pump_starts,
            SZ_DHW_BURNER_HOURS: self.opentherm_state.counters.dhw_burner_hours,
            SZ_DHW_BURNER_STARTS: self.opentherm_state.counters.dhw_burner_starts,
            SZ_DHW_PUMP_HOURS: self.opentherm_state.counters.dhw_pump_hours,
            SZ_DHW_PUMP_STARTS: self.opentherm_state.counters.dhw_pump_starts,
            SZ_FLAME_SIGNAL_LOW: self.opentherm_state.counters.flame_signal_low,
        }  # 0x73 is not a counter: is OEM diagnostic code...

    async def opentherm_params(
        self,
    ) -> SchemaT:  # F8_8, U8, {"hb": S8, "lb": S8}
        """Return OpenTherm configuration parameter dictionary."""
        return {}

    async def ramses_schema(self) -> PayDictT.EMPTY:
        """Return RAMSES-II schema configuration dictionary."""
        return {}

    async def ramses_params(self) -> dict[str, float | None]:
        """Return RAMSES-II parameter configuration dictionary."""
        return {
            SZ_MAX_REL_MODULATION: await self.max_rel_modulation(),
        }

    async def traits(self) -> SchemaT:
        """Return device traits dictionary."""
        return await super().traits()

    async def opentherm_state_dto(self) -> OpenThermStateDTO | None:
        """Return the OpenTherm Bridge state as a CQRS DTO.

        :returns: OpenThermStateDTO or None.
        :rtype: OpenThermStateDTO | None
        """
        state = getattr(self, "opentherm_state", None)
        if not state:
            return None

        return OpenThermStateDTO(
            flags=state.flags,
            faults=state.faults,
            temperatures=state.temperatures,
            counters=state.counters,
            ch_water_pressure=state.ch_water_pressure,
            dhw_flow_rate=state.dhw_flow_rate,
            max_rel_modulation=state.max_rel_modulation,
            rel_modulation_level=state.rel_modulation_level,
            oem_code=state.oem_code,
            oem_fault_code=state.oem_fault_code,
            last_updated=state.last_updated,
        )

    async def schema(self) -> SchemaT:
        """Return combined device configuration schema."""
        base_schema = await super().schema()
        return {
            **base_schema,
            SZ_OPENTHERM_SCHEMA: await self.opentherm_schema(),
            SZ_RAMSES_II_SCHEMA: await self.ramses_schema(),
        }

    async def params(self) -> SchemaT:
        """Return combined device parameters dictionary."""
        base_params = await super().params()
        return {
            **base_params,
            SZ_OPENTHERM_PARAMS: await self.opentherm_params(),
            SZ_RAMSES_II_PARAMS: await self.ramses_params(),
        }

    async def status(self) -> SchemaT:
        """Return combined operational status dictionary."""
        base_status = await super().status()
        return {
            **base_status,  # incl. heat_demand
            self.OPENTHERM_STATE: await self.opentherm_state_dto(),
            SZ_BOILER_EXHAUST_TEMP: await self.boiler_exhaust_temp(),
            SZ_BOILER_OUTPUT_TEMP: await self.boiler_output_temp(),
            SZ_BOILER_RETURN_TEMP: await self.boiler_return_temp(),
            SZ_BOILER_SETPOINT: await self.boiler_setpoint(),
            SZ_CH_SETPOINT: await self.ch_setpoint(),
            SZ_CH_MAX_SETPOINT: await self.ch_max_setpoint(),
            SZ_CH_WATER_PRESSURE: await self.ch_water_pressure(),
            SZ_DHW_FLOW_RATE: await self.dhw_flow_rate(),
            SZ_DHW_SETPOINT: await self.dhw_setpoint(),
            SZ_DHW_TEMP: await self.dhw_temp(),
            SZ_OEM_CODE: await self.oem_code(),
            SZ_OEM_FAULT_CODE: await self.oem_fault_code(),
            SZ_OUTSIDE_TEMP: await self.outside_temp(),
            SZ_REL_MODULATION_LEVEL: await self.rel_modulation_level(),
            SZ_CH_ACTIVE: await self.ch_active(),
            SZ_CH_ENABLED: await self.ch_enabled(),
            SZ_COOLING_ACTIVE: await self.cooling_active(),
            SZ_COOLING_ENABLED: await self.cooling_enabled(),
            SZ_DHW_ACTIVE: await self.dhw_active(),
            SZ_DHW_BLOCKING: await self.dhw_blocking(),
            SZ_DHW_ENABLED: await self.dhw_enabled(),
            SZ_FAULT_PRESENT: await self.fault_present(),
            SZ_FLAME_ACTIVE: await self.flame_active(),
            SZ_SUMMER_MODE: await self.summer_mode(),
            SZ_OTC_ACTIVE: await self.otc_active(),
            SZ_SERVICE_REQUEST: await self.service_request(),
            SZ_LOCKOUT_RESET: await self.lockout_reset(),
            SZ_LOW_WATER_PRESSURE: await self.low_water_pressure(),
            SZ_GAS_FLAME_FAULT: await self.gas_flame_fault(),
            SZ_AIR_PRESSURE_FAULT: await self.air_pressure_fault(),
            SZ_WATER_OVER_TEMPERATURE: await self.water_over_temperature(),
        }
