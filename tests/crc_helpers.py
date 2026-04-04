"""Shared CRC-32 helpers for test_demod_ber.py and test_demod_timing.py."""
from typing import List


def _build_crc32_table() -> List[int]:
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (0xEDB88320 ^ (c >> 1)) if (c & 1) else (c >> 1)
        table.append(c)
    return table


_CRC32_TABLE = _build_crc32_table()


def crc32_bytes(data: bytes) -> int:
    """Compute CRC-32 (IEEE 802.3) of a bytes object."""
    crc = 0xFFFFFFFF
    for b in data:
        crc = _CRC32_TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def append_crc32(bits: List[int]) -> List[int]:
    """Append 32 CRC bits (big-endian) to a bit sequence."""
    n = len(bits)
    data = bytearray()
    for i in range(0, n, 8):
        byte = 0
        for j in range(8):
            if i + j < n:
                byte = (byte << 1) | (bits[i + j] & 1)
        data.append(byte)
    crc = crc32_bytes(bytes(data))
    return bits + [(crc >> (31 - i)) & 1 for i in range(32)]
