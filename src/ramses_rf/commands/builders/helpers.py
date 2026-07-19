"""RAMSES RF - Intent-to-DTO Translation Helpers."""

from ramses_rf.address import Address


def resolve_addrs(src: Address | str, dst: Address | str) -> tuple[str, str, str]:
    """Resolve logical source and destination to positional MAC addresses.

    :param src: Logical source of the command.
    :param dst: Logical target of the command.
    :return: A tuple of (addr1, addr2, addr3) for the L3 CommandDTO.
    """
    src_id = src if isinstance(src, str) else src.id
    dst_id = dst if isinstance(dst, str) else dst.id

    if src_id == dst_id:
        return src_id, "--:------", dst_id
    return src_id, dst_id, "--:------"
