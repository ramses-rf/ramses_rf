#!/usr/bin/env python3
"""RAMSES RF - Test the Command.put_*, Command.set_* APIs."""

from ramses_tx import exceptions as exc
from ramses_tx.command import CODE_API_MAP, Command

EXCLUDED_APIS = ("from_attrs", "_from_attrs", "from_cli", "get_fan_param")
EXCLUDED_APIS += ()  # APIs not added to the CODE_API_MAP, should be an empty tuple


def test_command_apis_via_map() -> None:
    """Check that all Command constructors are in CODE_API_MAP."""

    cls_apis = {
        v.__name__
        for k, v in Command.__dict__.items()
        if isinstance(v, classmethod) and k[:1] != "_" and k not in EXCLUDED_APIS
    }

    map_apis = {v.__name__ for v in CODE_API_MAP.values()}

    assert not map_apis.symmetric_difference(cls_apis)


def test_1fc9_constructors_fail() -> None:
    """Check the 1FC9 Command constructors behave as expected when given bad params."""

    try:
        _ = Command.put_bind(" I", "29:156898", None)  # should have codes, or dst_id
    except exc.CommandInvalid:
        pass
    else:
        assert False


def test_1fc9_constructors_good() -> None:
    """Check the 1FC9 Command constructors behave as expected when give good params."""

    #
    # SWI switch (22F1/3) binding to a FAN (31D9/A)?
    frame = " I --- 37:155617 --:------ 37:155617 1FC9 024 0022F1965FE10022F3965FE16710E0965FE1001FC9965FE1"
    cmd = Command.put_bind(" I", "37:155617", ("22F1", "22F3"), oem_code="67")
    assert cmd._frame == frame

    frame = " W --- 32:132125 29:156898 --:------ 1FC9 012 0031D982041D0031DA82041D"
    cmd = Command.put_bind(" W", "32:132125", ("31D9", "31DA"), dst_id="29:156898")
    assert cmd._frame == frame

    frame = " I --- 29:156898 32:132125 --:------ 1FC9 001 00"
    cmd = Command.put_bind(" I", "29:156898", None, dst_id="32:132125")
    assert cmd._frame == frame

    #
    # CO2 remote (1298/31E0, 2E10) binding to a FAN (31D9/A)
    frame = " I --- 37:154011 --:------ 37:154011 1FC9 030 0031E096599B00129896599B002E1096599B0110E096599B001FC996599B"
    cmd = Command.put_bind(" I", "37:154011", ("31E0", "1298", "2E10"), oem_code="01")
    assert cmd._frame == frame

    frame = " W --- 18:126620 37:154011 --:------ 1FC9 012 0031D949EE9C0031DA49EE9C"
    cmd = Command.put_bind(" W", "18:126620", ("31D9", "31DA"), dst_id="37:154011")
    assert cmd._frame == frame

    frame = " I --- 37:154011 18:126620 --:------ 1FC9 001 00"
    cmd = Command.put_bind(" I", "37:154011", None, dst_id="18:126620")
    assert cmd._frame == frame

    #
    # STA binding to a CTL as a thermostat (2309, 30C9, 0008, 1FC9): zone idx 08
    frame = " I --- 12:010740 --:------ 12:010740 1FC9 024 0023093029F40030C93029F40000083029F4001FC93029F4"
    cmd = Command.put_bind(" I", "12:010740", ("2309", "30C9", "0008"))
    assert cmd._frame == frame

    frame = " W --- 01:145038 12:010740 --:------ 1FC9 006 08230906368E"
    cmd = Command.put_bind(" W", "01:145038", ("2309",), dst_id="12:010740", idx="08")
    assert cmd._frame == frame

    frame = " I --- 12:010740 01:145038 --:------ 1FC9 006 0023093029F4"
    cmd = Command.put_bind(" I", "12:010740", ("2309",), dst_id="01:145038")
    assert cmd._frame == frame

    #
    # DHW sensor binding to a CTL (1260, 1FC9): dhw_idx 00
    frame = " I --- 07:045960 --:------ 07:045960 1FC9 012 0012601CB388001FC91CB388"
    cmd = Command.put_bind(" I", "07:045960", "1260")
    assert cmd._frame == frame  # using str for codes

    frame = " W --- 01:145038 07:045960 --:------ 1FC9 006 0010A006368E"
    cmd = Command.put_bind(" W", "01:145038", "10A0", dst_id="07:045960")
    assert cmd._frame == frame  # using str for codes

    frame = " I --- 07:045960 01:145038 --:------ 1FC9 006 0012601CB388"
    cmd = Command.put_bind(" I", "07:045960", "1260", dst_id="01:145038")
    assert cmd._frame == frame  # using str for codes

    # NOTE: the APIs are not (yet) intended for these edge-case packets
    # TRV binding to a CTL (2309, 30C9, 1FC9): zone idx 07 - NOTE: counter-offer pkt!
    # # frame = " I --- 04:189076 63:262142 --:------ 1FC9 006 0030C912E294"
    # # cmd = Command.put_bind(" I", "04:189076", ("30C9",), dst_id="63:262142")
    # # assert cmd._frame == frame  # NOTE: NUL-ADDR, and there is no 1FC9 in the payload!

    # # frame = " I --- 01:145038 --:------ 01:145038 1FC9 018 07230906368E0730C906368E071FC906368E"
    # # cmd = Command.put_bind(" I", "01:145038", ("2309", "30C9"), idx="07")
    # # assert cmd._frame == frame  # NOTE: this is the counter-offer

    frame = " W --- 04:189076 01:145038 --:------ 1FC9 006 0030C912E294"
    cmd = Command.put_bind(" W", "04:189076", ("30C9",), dst_id="01:145038")
    assert cmd._frame == frame

    frame = " I --- 01:145038 04:189076 --:------ 1FC9 006 00FFFF06368E"
    cmd = Command.put_bind(" I", "01:145038", "FFFF", dst_id="04:189076")
    assert cmd._frame == frame  # using SENTINEL str for codes
