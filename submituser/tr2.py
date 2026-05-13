#!/usr/bin/env python3
# rtp_crate_streamer.py
#
# Stream UDP "RTP-like" per 1 crate:
#   - 7 board per crate
#   - 12 stream analogici per board
#   - 1 stream digitale aggregato per board (16 bit digitali)
#   - totale: 91 stream RTP per crate
#
# Formato payload per campione (4 byte), compatibile col tuo script attuale:
#   [MSB][MID][LSB][channel_id]
#
# Analogico:
#   sample24 = ADC offset-binary 24 bit
#
# Digitale aggregato:
#   sample24 = 0x00 | digital[15:8] | digital[7:0]
#   cioè i 16 bit digitali stanno nei 16 bit meno significativi del sample24.
#
# In fondo al payload c'è 1 byte finale 0x00.
#
# Uso:
#   python3 rtp_crate_streamer.py <dst_ip> <crate> [sample_rate_hz] [dst_port]
#
# Esempi:
#   python3 rtp_crate_streamer.py 192.168.1.10 3 5000 6666
#   python3 rtp_crate_streamer.py 127.0.0.1 1 1000 6666
#
# Note:
# - board ids usati: 0..6
# - canali analogici: 0..11
# - stream digitale aggregato: channel=12
# - SSRC:
#     [ prefix(16)=0xBDAC | crate(8) | half(1)=0 | board(3) | channel(4) ]

import argparse
import heapq
import logging
import random
import socket
import struct
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional


PREFIX = 0xBDAC
HALF = 0

BOARDS_PER_CRATE = 7
ANALOG_CHANNELS_PER_BOARD = 12
DIGITAL_STREAM_CHANNEL = 12   # 13-esimo stream del board
TOTAL_STREAMS_PER_CRATE = BOARDS_PER_CRATE * (ANALOG_CHANNELS_PER_BOARD + 1)

SAMPLES_PER_PACKET = 180
RTP_HEADER_LEN = 12
CSRC_LEN = 4
PAYLOAD_LEN = SAMPLES_PER_PACKET * 4 + 1
PACKET_LEN = RTP_HEADER_LEN + CSRC_LEN + PAYLOAD_LEN

DEFAULT_PORT = 6666
DEFAULT_PAYLOAD_TYPE = 23
DEFAULT_CSRC0 = 0x800005DC

MAX24 = 16777215.0  # 2^24 - 1


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def volts_to_u24_offset(v: float, full_scale_volts: float) -> int:
    """
    Mappa [-FS, +FS] su [0 .. 0xFFFFFF] in offset-binary.
    """
    v = clamp(v, -full_scale_volts, full_scale_volts)
    span = 2.0 * full_scale_volts
    norm = (v + full_scale_volts) / span
    code = norm * MAX24
    code = clamp(code, 0.0, MAX24)
    return int(round(code))


def build_ssrc(crate: int, board: int, channel: int, half: int = HALF, prefix: int = PREFIX) -> int:
    if not (0 <= crate <= 0xFF):
        raise ValueError("crate fuori range (0..255)")
    if not (0 <= board <= 0x7):
        raise ValueError("board fuori range (0..7)")
    if not (0 <= channel <= 0xF):
        raise ValueError("channel fuori range (0..15)")
    if half not in (0, 1):
        raise ValueError("half deve essere 0 o 1")

    return (
        ((prefix & 0xFFFF) << 16) |
        ((crate & 0xFF) << 8) |
        ((half & 0x1) << 7) |
        ((board & 0x7) << 4) |
        (channel & 0xF)
    )


@dataclass
class Config:
    dst_ip: str
    crate: int
    sample_rate_hz: int = 5000
    dst_port: int = DEFAULT_PORT

    samples_per_packet: int = SAMPLES_PER_PACKET
    payload_type: int = DEFAULT_PAYLOAD_TYPE
    csrc0: int = DEFAULT_CSRC0
    marker: bool = False

    full_scale_volts: float = 10.0
    noise_rms_volts: float = 0.01

    report_interval_s: float = 60.0

    # 0 = infinito
    packets_per_stream: int = 0

    # Per lo stream digitale: probabilità di flip di ciascun bit per sample
    digital_flip_probability: float = 0.002

    @property
    def packets_per_second_per_stream(self) -> float:
        return self.sample_rate_hz / self.samples_per_packet

    @property
    def period_per_stream_s(self) -> float:
        return self.samples_per_packet / self.sample_rate_hz

    @property
    def total_expected_pps(self) -> float:
        return TOTAL_STREAMS_PER_CRATE * self.packets_per_second_per_stream

    @property
    def expected_throughput_mbps(self) -> float:
        return (PACKET_LEN * self.total_expected_pps * 8.0) / 1e6


@dataclass
class StreamState:
    kind: str                  # "analog" o "digital"
    board: int
    channel: int
    ssrc: int
    channel_id: int
    packet: bytearray
    seq: int = 0
    ts: int = 0
    next_send: float = 0.0
    sent_packets: int = 0
    io_errors: int = 0
    digital_state: int = 0


@dataclass
class Stats:
    start_time: float

    total_packets_sent: int = 0
    total_bytes_sent: int = 0

    total_send_errors: int = 0
    total_deadline_misses: int = 0
    total_packets_dropped_timing: int = 0
    total_packets_dropped_io: int = 0
    total_samples_lost: int = 0

    total_late_sends: int = 0
    worst_lateness_s: float = 0.0

    win_packets_sent: int = 0
    win_bytes_sent: int = 0
    win_send_errors: int = 0
    win_deadline_misses: int = 0
    win_packets_dropped_timing: int = 0
    win_packets_dropped_io: int = 0
    win_samples_lost: int = 0
    win_late_sends: int = 0
    win_worst_lateness_s: float = 0.0

    def note_late(self, lateness_s: float) -> None:
        if lateness_s <= 0:
            return
        self.total_late_sends += 1
        self.win_late_sends += 1
        if lateness_s > self.worst_lateness_s:
            self.worst_lateness_s = lateness_s
        if lateness_s > self.win_worst_lateness_s:
            self.win_worst_lateness_s = lateness_s

    def note_timing_drop(self, missed_packets: int, samples_per_packet: int) -> None:
        if missed_packets <= 0:
            return
        lost_samples = missed_packets * samples_per_packet

        self.total_deadline_misses += 1
        self.win_deadline_misses += 1

        self.total_packets_dropped_timing += missed_packets
        self.win_packets_dropped_timing += missed_packets

        self.total_samples_lost += lost_samples
        self.win_samples_lost += lost_samples

    def note_send_ok(self, packet_len: int) -> None:
        self.total_packets_sent += 1
        self.win_packets_sent += 1
        self.total_bytes_sent += packet_len
        self.win_bytes_sent += packet_len

    def note_send_error(self, samples_per_packet: int) -> None:
        self.total_send_errors += 1
        self.win_send_errors += 1

        self.total_packets_dropped_io += 1
        self.win_packets_dropped_io += 1

        self.total_samples_lost += samples_per_packet
        self.win_samples_lost += samples_per_packet

    def reset_window(self) -> None:
        self.win_packets_sent = 0
        self.win_bytes_sent = 0
        self.win_send_errors = 0
        self.win_deadline_misses = 0
        self.win_packets_dropped_timing = 0
        self.win_packets_dropped_io = 0
        self.win_samples_lost = 0
        self.win_late_sends = 0
        self.win_worst_lateness_s = 0.0


def build_packet_template(cfg: Config, ssrc: int) -> bytearray:
    pkt = bytearray(PACKET_LEN)

    # RTP fixed header:
    # V=2, P=0, X=0, CC=1  => 0x81
    pkt[0] = 0x81
    pkt[1] = (0x80 if cfg.marker else 0x00) | (cfg.payload_type & 0x7F)

    # seq, ts verranno aggiornati ad ogni invio
    struct.pack_into(">I", pkt, 8, ssrc & 0xFFFFFFFF)
    struct.pack_into(">I", pkt, 12, cfg.csrc0 & 0xFFFFFFFF)

    # trailing byte
    pkt[-1] = 0x00

    return pkt


def fill_analog_payload(pkt: bytearray, channel_id: int, cfg: Config, rng: random.Random) -> None:
    pl = memoryview(pkt)[RTP_HEADER_LEN + CSRC_LEN:]

    sigma = cfg.noise_rms_volts
    fs = cfg.full_scale_volts

    for k in range(cfg.samples_per_packet):
        v = rng.gauss(0.0, sigma)
        u24 = volts_to_u24_offset(v, fs)

        base = k * 4
        pl[base + 0] = (u24 >> 16) & 0xFF
        pl[base + 1] = (u24 >> 8) & 0xFF
        pl[base + 2] = u24 & 0xFF
        pl[base + 3] = channel_id

    pl[cfg.samples_per_packet * 4] = 0x00


def fill_digital_payload(pkt: bytearray, channel_id: int, stream: StreamState, cfg: Config, rng: random.Random) -> None:
    """
    Ogni sample contiene 16 bit digitali aggregati del board.
    Li codifichiamo come valore 24 bit:
      u24 = 0x00XXXX
    dove XXXX sono i 16 bit digitali.
    """
    pl = memoryview(pkt)[RTP_HEADER_LEN + CSRC_LEN:]
    state = stream.digital_state
    pflip = cfg.digital_flip_probability

    for k in range(cfg.samples_per_packet):
        mask = 0
        for bit in range(16):
            if rng.random() < pflip:
                mask |= (1 << bit)

        state ^= mask
        u24 = state & 0xFFFF

        base = k * 4
        pl[base + 0] = (u24 >> 16) & 0xFF   # sempre 0 per ora
        pl[base + 1] = (u24 >> 8) & 0xFF
        pl[base + 2] = u24 & 0xFF
        pl[base + 3] = channel_id

    pl[cfg.samples_per_packet * 4] = 0x00
    stream.digital_state = state


def build_streams(cfg: Config, start_time: float) -> List[StreamState]:
    streams: List[StreamState] = []
    seq_seed = 1

    # Se i tuoi board devono essere 1..7 invece di 0..6, cambia qui.
    board_ids = range(BOARDS_PER_CRATE)

    for board in board_ids:
        for ch in range(ANALOG_CHANNELS_PER_BOARD):
            ssrc = build_ssrc(cfg.crate, board, ch)
            pkt = build_packet_template(cfg, ssrc)

            streams.append(
                StreamState(
                    kind="analog",
                    board=board,
                    channel=ch,
                    ssrc=ssrc,
                    channel_id=ssrc & 0xFF,
                    packet=pkt,
                    seq=seq_seed & 0xFFFF,
                    ts=0,
                )
            )
            seq_seed += 1

        ssrc = build_ssrc(cfg.crate, board, DIGITAL_STREAM_CHANNEL)
        pkt = build_packet_template(cfg, ssrc)

        streams.append(
            StreamState(
                kind="digital",
                board=board,
                channel=DIGITAL_STREAM_CHANNEL,
                ssrc=ssrc,
                channel_id=ssrc & 0xFF,
                packet=pkt,
                seq=seq_seed & 0xFFFF,
                ts=0,
            )
        )
        seq_seed += 1

    period = cfg.period_per_stream_s

    # Staggering iniziale per non sparare tutti gli stream nello stesso istante
    for i, s in enumerate(streams):
        phase = (i / len(streams)) * period
        s.next_send = start_time + phase

    return streams


def report_stats(cfg: Config, stats: Stats, elapsed_win_s: float) -> None:
    total_elapsed = max(time.perf_counter() - stats.start_time, 1e-9)

    win_mbps = (stats.win_bytes_sent * 8.0) / (elapsed_win_s * 1e6) if elapsed_win_s > 0 else 0.0
    tot_mbps = (stats.total_bytes_sent * 8.0) / (total_elapsed * 1e6)

    win_pps = stats.win_packets_sent / elapsed_win_s if elapsed_win_s > 0 else 0.0
    tot_pps = stats.total_packets_sent / total_elapsed

    logging.info(
        "STAT  crate=%d  sent=%d pkt  %.3f MB  win=%.3f Mbps %.1f pkt/s  "
        "tot=%.3f Mbps %.1f pkt/s  late(win/tot)=%d/%d  worst_late(win/tot)=%.3f/%.3f ms  "
        "drop_timing(win/tot)=%d/%d pkt  drop_io(win/tot)=%d/%d pkt  lost_samples(win/tot)=%d/%d  send_errors(win/tot)=%d/%d",
        cfg.crate,
        stats.total_packets_sent,
        stats.total_bytes_sent / (1024.0 * 1024.0),
        win_mbps,
        win_pps,
        tot_mbps,
        tot_pps,
        stats.win_late_sends,
        stats.total_late_sends,
        stats.win_worst_lateness_s * 1e3,
        stats.worst_lateness_s * 1e3,
        stats.win_packets_dropped_timing,
        stats.total_packets_dropped_timing,
        stats.win_packets_dropped_io,
        stats.total_packets_dropped_io,
        stats.win_samples_lost,
        stats.total_samples_lost,
        stats.win_send_errors,
        stats.total_send_errors,
    )

    stats.reset_window()


def send_one_packet(
    sock: socket.socket,
    cfg: Config,
    stats: Stats,
    stream: StreamState,
    rng: random.Random,
    now: float,
) -> None:
    period = cfg.period_per_stream_s
    lateness = now - stream.next_send

    if lateness > 0:
        stats.note_late(lateness)

    missed_packets = 0
    if lateness >= period:
        missed_packets = int(lateness / period)

    if missed_packets > 0:
        stats.note_timing_drop(missed_packets, cfg.samples_per_packet)

        # salta i pacchetti non riusciti a schedulare
        stream.seq = (stream.seq + missed_packets) & 0xFFFF
        stream.ts = (stream.ts + missed_packets * cfg.samples_per_packet) & 0xFFFFFFFF
        stream.next_send += missed_packets * period

    struct.pack_into(">H", stream.packet, 2, stream.seq & 0xFFFF)
    struct.pack_into(">I", stream.packet, 4, stream.ts & 0xFFFFFFFF)

    if stream.kind == "analog":
        fill_analog_payload(stream.packet, stream.channel_id, cfg, rng)
    else:
        fill_digital_payload(stream.packet, stream.channel_id, stream, cfg, rng)

    try:
        sent = sock.sendto(stream.packet, (cfg.dst_ip, cfg.dst_port))
        if sent != len(stream.packet):
            logging.error(
                "Invio parziale: board=%d channel=%d kind=%s sent=%d expected=%d",
                stream.board, stream.channel, stream.kind, sent, len(stream.packet)
            )
            stats.note_send_error(cfg.samples_per_packet)
            stream.io_errors += 1
        else:
            stats.note_send_ok(sent)
            stream.sent_packets += 1
    except OSError as exc:
        logging.error(
            "sendto() fallita: board=%d channel=%d kind=%s seq=%d ts=%d err=%s",
            stream.board, stream.channel, stream.kind, stream.seq, stream.ts, exc
        )
        stats.note_send_error(cfg.samples_per_packet)
        stream.io_errors += 1

    stream.seq = (stream.seq + 1) & 0xFFFF
    stream.ts = (stream.ts + cfg.samples_per_packet) & 0xFFFFFFFF
    stream.next_send += period


def parse_args(argv: List[str]) -> Config:
    parser = argparse.ArgumentParser(
        description="Streamer RTP-like per crate: 7 board, 12 analog + 1 digital stream per board."
    )
    parser.add_argument("dst_ip", help="IP destinazione")
    parser.add_argument("crate", type=int, help="Numero crate (0..255)")
    parser.add_argument(
        "sample_rate_hz",
        nargs="?",
        type=int,
        default=5000,
        choices=[1000, 5000],
        help="Frequenza per stream in Hz: 1000 o 5000 (default: 5000)"
    )
    parser.add_argument(
        "dst_port",
        nargs="?",
        type=int,
        default=DEFAULT_PORT,
        help=f"Porta UDP destinazione (default: {DEFAULT_PORT})"
    )

    parser.add_argument("--payload-type", type=int, default=DEFAULT_PAYLOAD_TYPE)
    parser.add_argument("--csrc0", type=lambda x: int(x, 0), default=DEFAULT_CSRC0)
    parser.add_argument("--fs", type=float, default=10.0, help="Full scale analogico in Volt (default: 10.0)")
    parser.add_argument("--noise-rms", type=float, default=0.01, help="Rumore analogico RMS in Volt (default: 0.01)")
    parser.add_argument("--report-interval", type=float, default=60.0, help="Intervallo report in secondi (default: 60)")
    parser.add_argument("--packets-per-stream", type=int, default=0, help="0 = infinito, altrimenti stop dopo N pacchetti per stream")
    parser.add_argument("--digital-flip-prob", type=float, default=0.002, help="Probabilità flip per bit digitale per sample")
    parser.add_argument("--marker", action="store_true", help="Imposta marker bit RTP")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    ns = parser.parse_args(argv[1:])

    return Config(
        dst_ip=ns.dst_ip,
        crate=ns.crate,
        sample_rate_hz=ns.sample_rate_hz,
        dst_port=ns.dst_port,
        payload_type=ns.payload_type,
        csrc0=ns.csrc0,
        marker=ns.marker,
        full_scale_volts=ns.fs,
        noise_rms_volts=ns.noise_rms,
        report_interval_s=ns.report_interval,
        packets_per_stream=ns.packets_per_stream,
        digital_flip_probability=ns.digital_flip_prob,
    )


def main(argv: List[str]) -> int:
    cfg = parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger().setLevel(getattr(logging, cfg.__dict__.get("log_level", "INFO"), logging.INFO))

    rng = random.Random()

    start = time.perf_counter()
    stats = Stats(start_time=start)

    streams = build_streams(cfg, start)
    heap = [(s.next_send, idx) for idx, s in enumerate(streams)]
    heapq.heapify(heap)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)

    logging.info("Start crate=%d dst=%s:%d", cfg.crate, cfg.dst_ip, cfg.dst_port)
    logging.info(
        "Streams=%d  board=%d  analog/board=%d  digital_streams/board=1",
        len(streams), BOARDS_PER_CRATE, ANALOG_CHANNELS_PER_BOARD
    )
    logging.info(
        "Sample rate=%d Hz  samples/pkt=%d  pps/stream=%.6f  total_pps=%.3f",
        cfg.sample_rate_hz, cfg.samples_per_packet,
        cfg.packets_per_second_per_stream, cfg.total_expected_pps
    )
    logging.info(
        "Packet len=%d B  expected throughput=%.3f Mbps",
        PACKET_LEN, cfg.expected_throughput_mbps
    )
    logging.info(
        "Analog FS=+/-%.3f V  noise_rms=%.6f V  digital_flip_prob=%.6f",
        cfg.full_scale_volts, cfg.noise_rms_volts, cfg.digital_flip_probability
    )

    next_report = start + cfg.report_interval_s
    last_report = start

    try:
        while heap:
            now = time.perf_counter()

            while now >= next_report:
                report_stats(cfg, stats, now - last_report)
                last_report = now
                next_report += cfg.report_interval_s

            next_send, idx = heap[0]

            if now < next_send:
                sleep_until = min(next_send, next_report)
                sleep_for = sleep_until - now
                if sleep_for > 0:
                    time.sleep(sleep_for)
                continue

            _, idx = heapq.heappop(heap)
            stream = streams[idx]

            if cfg.packets_per_stream > 0 and stream.sent_packets >= cfg.packets_per_stream:
                done = True
                for s in streams:
                    if s.sent_packets < cfg.packets_per_stream:
                        done = False
                        break
                if done:
                    break
                continue

            send_one_packet(sock, cfg, stats, stream, rng, now)

            if cfg.packets_per_stream == 0 or stream.sent_packets < cfg.packets_per_stream:
                heapq.heappush(heap, (stream.next_send, idx))

    except KeyboardInterrupt:
        logging.warning("Interrotto da tastiera")

    finally:
        now = time.perf_counter()
        if now > last_report:
            report_stats(cfg, stats, now - last_report)
        sock.close()

    logging.info("Terminato")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
