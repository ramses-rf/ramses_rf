#!/usr/bin/env python3
"""RAMSES RF - Test the Command.put_*, Command.set_* APIs."""

from ramses_rf.address import Address
from ramses_rf.commands.builders import build_dto
from ramses_rf.commands.core import Command as Intent
from ramses_rf.enums import Action
from ramses_tx import exceptions as exc
from ramses_tx.command import CODE_API_MAP, Command
from ramses_tx.command_legacy_shim import LegacyCommandShim

EXCLUDED_APIS = ("from_attrs", "_from_attrs", "from_cli")
EXCLUDED_APIS += ()  # APIs not added to the CODE_API_MAP, should be an empty tuple


def put_bind(
    verb: str,
    src_id: str,
    codes: tuple | str | None,
    dst_id: str | None = None,
    idx: str = "00",
    oem_code: str | None = None,
) -> Command:
    return LegacyCommandShim.from_dto(
        build_dto(
            Intent(
                src=Address(src_id),
                dst=Address(dst_id or src_id),
                action=Action.PUT_BIND,
                data={"verb": verb, "codes": codes, "idx": idx, "oem_code": oem_code},
            )
        )
    )


def test_api_map_is_complete() -> None:
    """Check that all Command constructors are in CODE_API_MAP."""

    cls_apis = set()
    # Iterate through the class hierarchy to pick up all Mixin methods
    for base in Command.__mro__:
        # Stop before picking up base constructors from Frame or Python object
        if base.__name__ == "Frame" or base is object:
            continue
        for name, attr in base.__dict__.items():
            if (
                isinstance(attr, classmethod)
                and name[:1] != "_"
                and name not in EXCLUDED_APIS
            ):
                cls_apis.add(name)

    map_apis = {v.__name__ for v in CODE_API_MAP.values()}

    assert not map_apis.symmetric_difference(cls_apis)


def test_1fc9_constructors_fail() -> None:
    """Check the 1FC9 Command constructors behave as expected when given bad params."""

    try:
        _ = put_bind(" I", "29:156898", None)  # should have codes, or dst_id
    except (exc.CommandInvalid, ValueError):
        pass
    else:
        assert False


def test_1fc9_constructors_good() -> None:
    """Check the 1FC9 Command constructors behave as expected when give good params."""

    #
    # SWI switch (22F1/3) binding to a FAN (31D9/A)?
    frame = " I --- 37:155617 --:------ 37:155617 1FC9 024 0022F1965FE10022F3965FE16710E0965FE1001FC9965FE1"
    cmd = put_bind(" I", "37:155617", ("22F1", "22F3"), oem_code="67")
    assert cmd._frame == frame

    frame = " W --- 32:132125 29:156898 --:------ 1FC9 012 0031D982041D0031DA82041D"
    cmd = put_bind(" W", "32:132125", ("31D9", "31DA"), dst_id="29:156898")
    assert cmd._frame == frame

    frame = " I --- 29:156898 32:132125 --:------ 1FC9 001 00"
    cmd = put_bind(" I", "29:156898", None, dst_id="32:132125")
    assert cmd._frame == frame

    #
    # CO2 remote (1298/31E0, 2E10) binding to a FAN (31D9/A)
    frame = " I --- 37:154011 --:------ 37:154011 1FC9 030 0031E096599B00129896599B002E1096599B0110E096599B001FC996599B"
    cmd = put_bind(" I", "37:154011", ("31E0", "1298", "2E10"), oem_code="01")
    assert cmd._frame == frame

    frame = " W --- 18:126620 37:154011 --:------ 1FC9 012 0031D949EE9C0031DA49EE9C"
    cmd = put_bind(" W", "18:126620", ("31D9", "31DA"), dst_id="37:154011")
    assert cmd._frame == frame

    frame = " I --- 37:154011 18:126620 --:------ 1FC9 001 00"
    cmd = put_bind(" I", "37:154011", None, dst_id="18:126620")
    assert cmd._frame == frame

    #
    # STA binding to a CTL as a thermostat (2309, 30C9, 0008, 1FC9): zone idx 08
    frame = " I --- 12:010740 --:------ 12:010740 1FC9 024 0023093029F40030C93029F40000083029F4001FC93029F4"
    cmd = put_bind(" I", "12:010740", ("2309", "30C9", "0008"))
    assert cmd._frame == frame

    frame = " W --- 01:145038 12:010740 --:------ 1FC9 006 08230906368E"
    cmd = put_bind(" W", "01:145038", ("2309",), dst_id="12:010740", idx="08")
    assert cmd._frame == frame

    frame = " I --- 12:010740 01:145038 --:------ 1FC9 006 0023093029F4"
    cmd = put_bind(" I", "12:010740", ("2309",), dst_id="01:145038")
    assert cmd._frame == frame

    #
    # DHW sensor binding to a CTL (1260, 1FC9): dhw_idx 00
    frame = " I --- 07:045960 --:------ 07:045960 1FC9 012 0012601CB388001FC91CB388"
    cmd = put_bind(" I", "07:045960", "1260")
    assert cmd._frame == frame  # using str for codes

    frame = " W --- 01:145038 07:045960 --:------ 1FC9 006 0010A006368E"
    cmd = put_bind(" W", "01:145038", "10A0", dst_id="07:045960")
    assert cmd._frame == frame  # using str for codes

    frame = " I --- 07:045960 01:145038 --:------ 1FC9 006 0012601CB388"
    cmd = put_bind(" I", "07:045960", "1260", dst_id="01:145038")
    assert cmd._frame == frame  # using str for codes

    # NOTE: the APIs are not (yet) intended for these edge-case packets
    # TRV binding to a CTL (2309, 30C9, 1FC9): zone idx 07 - NOTE: counter-offer pkt!
    # # frame = " I --- 04:189076 63:262142 --:------ 1FC9 006 0030C912E294"
    # # cmd = put_bind(" I", "04:189076", ("30C9",), dst_id="63:262142")
    # # assert cmd._frame == frame  # NOTE: NUL-ADDR, and there is no 1FC9 in the payload!

    # # frame = " I --- 01:145038 --:------ 01:145038 1FC9 018 07230906368E0730C906368E071FC906368E"
    # # cmd = put_bind(" I", "01:145038", ("2309", "30C9"), idx="07")
    # # assert cmd._frame == frame  # NOTE: this is the counter-offer

    frame = " W --- 04:189076 01:145038 --:------ 1FC9 006 0030C912E294"
    cmd = put_bind(" W", "04:189076", ("30C9",), dst_id="01:145038")
    assert cmd._frame == frame

    frame = " I --- 01:145038 04:189076 --:------ 1FC9 006 00FFFF06368E"
    cmd = put_bind(" I", "01:145038", "FFFF", dst_id="04:189076")
    assert cmd._frame == frame  # using SENTINEL str for codes
