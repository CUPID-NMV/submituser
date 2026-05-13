#!/usr/bin/env python3
# rtp_noise_24bit.py
#
# Invio UDP di stream "RTP-like" con campioni 24-bit offset-binary e rumore gaussiano.
#
# Payload per campione (4 byte):
#   [channel_id][MSB][MID][LSB]   sample = 24-bit big-endian
# Più 1 byte finale 0x00.
#
# Uso:
#   python3 rtp_noise_24bit.py <dst_ip> [channels] [pps_per_channel] [dst_port]
#
# Esempio:
#   python3 rtp_noise_24bit.py 192.168.1.1 16 10 6666

import math
import random
import socket
import struct
import sys
import time
from dataclasses import dataclass
from typing import List


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def volts_to_u24_offset(v: float, full_scale_volts: float) -> int:
    """
    Map volts in [-FS, +FS] to 24-bit offset-binary [0 .. 0xFFFFFF]
      -FS -> 0x000000
      +FS -> 0xFFFFFF
    """
    v = clamp(v, -full_scale_volts, full_scale_volts)

    span = 2.0 * full_scale_volts          # es. 20V se FS=10
    norm = (v + full_scale_volts) / span   # 0..1
    max24 = 16777215.0                     # 2^24 - 1

    code = norm * max24
    code = clamp(code, 0.0, max24)

    return int(round(code))                # 0..0xFFFFFF


@dataclass
class ChannelState:
    ssrc: int
    seq: int
    ts: int
    channel_id: int


@dataclass
class Config:
    dst_ip: str
    dst_port: int = 5004

    channels: int = 8
    packets_per_channel: int = 1000

    samples_per_packet: int = 180
    ts_increment: int = 360
    payload_type: int = 23
    csrc0: int = 0x800005DC
    marker: bool = False

    full_scale_volts: float = 10.0
    noise_rms_volts: float = 0.01  # 10 mV RMS

    packets_per_second_per_channel: float = 10.0


def usage(prog: str) -> None:
    print(
        "Usage:\n"
        f"  {prog} <dst_ip> [channels] [pps_per_channel] [dst_port]\n\n"
        "Defaults:\n"
        "  channels=8, pps_per_channel=10, dst_port=6666",
        file=sys.stderr
    )


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        usage(argv[0])
        return 2

    cfg = Config(dst_ip=argv[1])

    if len(argv) >= 3:
        cfg.channels = max(1, int(argv[2]))
    if len(argv) >= 4:
        cfg.packets_per_second_per_channel = max(0.1, float(argv[3]))
    if len(argv) >= 5:
        cfg.dst_port = int(argv[4])
    else:
        cfg.dst_port = 6666  # come nel messaggio "Defaults" del C++

    # UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # RNG: gauss(0, sigma) dove sigma = RMS
    # random.gauss usa Box-Muller, va benissimo per questo.
    sigma = cfg.noise_rms_volts

    # Init canali (SSRC e seq diversi)
    channels: List[ChannelState] = []
    for i in range(cfg.channels):
        channels.append(
            ChannelState(
                ssrc=0xBDAC0000 + (i + 1),
                seq=1 + i,
                ts=0,
                channel_id=i & 0xFF
            )
        )

    # Lunghezze e buffer
    rtp_header_len = 12
    csrc_len = 4
    payload_len = cfg.samples_per_packet * 4 + 1
    pkt_len = rtp_header_len + csrc_len + payload_len

    pkt = bytearray(pkt_len)

    # RTP fisso
    # V=2,P=0,X=0,CC=1 => 0x80 | 0x01 = 0x81
    pkt[0] = 0x81
    # M + PT
    pkt[1] = (0x80 if cfg.marker else 0x00) | (cfg.payload_type & 0x7F)

    # CSRC[0] a offset 12..15 big-endian
    struct.pack_into(">I", pkt, 12, cfg.csrc0)

    per_chan_period = 1.0 / cfg.packets_per_second_per_channel

    # Note numeriche come nel C++
    span = 2.0 * cfg.full_scale_volts
    lsb_volts = span / 16777215.0
    noise_rms_lsb = cfg.noise_rms_volts / lsb_volts

    print("Sending RTP-like 24-bit offset-binary noise")
    print(f"  dst={cfg.dst_ip}:{cfg.dst_port}")
    print(f"  channels={cfg.channels}  pps/ch={cfg.packets_per_second_per_channel}  packets/ch={cfg.packets_per_channel}")
    print(f"  payload={payload_len} bytes ({cfg.samples_per_packet} samples *4 + 1)")
    print(f"  volts FS=+/-{cfg.full_scale_volts}V, noise={cfg.noise_rms_volts*1000.0:.3f} mV RMS (~{noise_rms_lsb:.2f} LSB RMS)")
    print(f"  RTP: PT={cfg.payload_type} CC=1 CSRC0=0x{cfg.csrc0:08X} ts_inc={cfg.ts_increment}")

    payload_offset = rtp_header_len + csrc_len

    for n in range(cfg.packets_per_channel):
        t0 = time.perf_counter()

        for s in channels:
            # Seq / TS / SSRC in header RTP
            struct.pack_into(">H", pkt, 2, s.seq & 0xFFFF)
            struct.pack_into(">I", pkt, 4, s.ts & 0xFFFFFFFF)
            struct.pack_into(">I", pkt, 8, s.ssrc & 0xFFFFFFFF)

            # Payload fill
            pl = pkt[payload_offset:]

            # per ogni campione: [channel_id][MSB][MID][LSB]
            # Payload fill
#             pl = memoryview(pkt)[payload_offset:]

#             for k in range(cfg.samples_per_packet):
#                v = random.gauss(0.0, sigma)
#                u24 = volts_to_u24_offset(v, cfg.full_scale_volts)

#                base = k * 4
#                pl[base + 0] = s.channel_id
#                pl[base + 1] = (u24 >> 16) & 0xFF
#                pl[base + 2] = (u24 >> 8) & 0xFF
#                pl[base + 3] = u24 & 0xFF

#             pl[cfg.samples_per_packet * 4] = 0x00
            
            pl = memoryview(pkt)[payload_offset:]

            channel_id = s.ssrc & 0xFF  # come nel PCAP

            for k in range(cfg.samples_per_packet):
                v = random.gauss(0.0, sigma)
                u24 = volts_to_u24_offset(v, cfg.full_scale_volts)

                base = k * 4
                pl[base + 0] = (u24 >> 16) & 0xFF  # MSB
                pl[base + 1] = (u24 >> 8)  & 0xFF  # MID
                pl[base + 2] = u24 & 0xFF          # LSB
                pl[base + 3] = channel_id          # channel_id (LSB SSRC)

            pl[cfg.samples_per_packet * 4] = 0x00
            
            
            # send
            sock.sendto(pkt, (cfg.dst_ip, cfg.dst_port))

            # advance
            s.seq = (s.seq + 1) & 0xFFFF
            s.ts = (s.ts + cfg.ts_increment) & 0xFFFFFFFF

        elapsed = time.perf_counter() - t0
        sleep_for = per_chan_period - elapsed
        if sleep_for > 0:
            time.sleep(sleep_for)

    sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
