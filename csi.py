"""RUV2 receive format from the WiFi DensePose ESP32-S3 firmware."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import struct

import numpy as np

HEADER = struct.Struct("<4sIIbbBBHH")
CARRIERS = np.r_[1:27, 38:64]


@dataclass(frozen=True)
class Frame:
    receiver: str
    source: str
    channel: int
    peer: bool
    sequence: int
    rssi: int
    iq: bytes

    @property
    def key(self):
        return f"{self.receiver}/{self.source}/{self.channel}/{int(self.peer)}"

    def profile(self):
        samples = np.frombuffer(self.iq, dtype=np.int8).reshape(64, 2)[CARRIERS]
        amplitude = np.hypot(samples[:, 0], samples[:, 1], dtype=np.float32)
        mean = amplitude.mean()
        return amplitude / mean if mean > 0 else None

    def wire_view(self, age):
        return {
            "id": self.key, "receiver": self.receiver, "source": self.source, "channel": self.channel,
            "peer": self.peer, "sequence": self.sequence, "rssi": self.rssi, "age_ms": round(age * 1000),
            "iq": base64.b64encode(self.iq).decode("ascii"),
        }


def from_udp(data: bytes, receiver: str) -> Frame | None:
    if len(data) != HEADER.size + 128 or not data.startswith(b"RUV2"):
        return None
    _, sequence, _, rssi, _, channel, flags, count, source = HEADER.unpack_from(data)
    if count != 64 or not 1 <= channel <= 14:
        return None
    return Frame(receiver, f"{source:04X}", channel, bool(flags & 8), sequence, rssi, data[HEADER.size:])


def from_visor(packet: dict) -> Frame | None:
    try:
        age_ms = packet["age_ms"]
        sequence = packet["sequence"]
        channel = packet["channel"]
        rssi = packet["rssi"]
        receiver = packet["receiver"]
        source = packet["source"]
        peer = packet["peer"]
        if (
            type(age_ms) is not int or not 0 <= age_ms <= 400 or
            type(sequence) is not int or not 0 <= sequence <= 0xffffffff or
            type(channel) is not int or not 1 <= channel <= 14 or
            type(rssi) is not int or not -128 <= rssi <= 127 or
            not isinstance(receiver, str) or not 0 < len(receiver) <= 46 or
            not isinstance(source, str) or len(source) != 4 or type(peer) is not bool or
            not isinstance(packet["iq"], str) or len(packet["iq"]) != 172
        ):
            return None
        iq = base64.b64decode(packet["iq"], validate=True)
    except (KeyError, TypeError, ValueError, binascii.Error):
        return None
    return Frame(receiver, source, channel, peer, sequence, rssi, iq) if len(iq) == 128 else None
