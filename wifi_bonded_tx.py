#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0
#
# Bonded WiFi TX — hard-split wideband transmission across two synchronized B210s.
#
# Design (hard split at the center seam)
# --------------------------------------
# A single 802.11a/g wideband baseband at `samp_rate` is split into two
# per-radio streams; radio A radiates the lower half, radio B the upper half.
#
# --split ofdm (default): ofdm_splitter splits every OFDM symbol by subcarrier
#   and synthesizes each radio's subcarriers at its own rate, so each radio
#   sends a complete OFDM signal and the halves stay orthogonal at the
#   receiver whatever the radios' relative phase (64-QAM included). Plan:
#   5/8 * samp_rate per radio at fc -/+ 12/64 * samp_rate (the bonded RX plan).
#   It finds each frame by the PHY's packet_len tag, so the PHY output goes
#   straight to it (no packet_pad2 in between).
# --split filter: band_splitter, a sharp split filter at fc; fine up to
#   16-QAM, but 64-QAM needs the radios' relative phase near 0
#   (--phase-ctl-port + tx_phase_cal.py). Plan: samp_rate/2 per radio at
#   fc -/+ 14/64 * samp_rate.
#
# The two radios share 10 MHz + PPS, so they are frequency-locked but each has
# its own LO. bonded_transmitter streams continuously (zero-fill between
# frames) so the B210 TX never ramps up mid-preamble.
#
# ALL TX DSP lives in C++:
#   - bonded_usrp.ofdm_splitter     wideband → two per-radio streams (per-symbol
#                                   subcarrier split; --split ofdm)
#   - bonded_usrp.band_splitter     the same with a split filter (--split filter)
#   - bonded_usrp.bonded_sink       synchronized 2-radio TX engine
#                                   (shared 10 MHz + PPS, timed start,
#                                   per-device sample-delay trim)
#
# This Python file is only a thin flowgraph; no DSP is done here.
#
# Usage
# -----
#   Terminal 1 — bonded TX (this script, Machine 1):
#       ./wifi_bonded_tx.py --samp-rate 10e6 --serial-a 30DBC3C --serial-b 30EDB63 --tx-gain 0.7
#   Terminal 2 — standard single-radio RX (no bonded code, Machine 2):
#       ./wifi_trx.py --mode rx --samp-rate 10e6 --rx-serial 30B56D6 --rx-gain 0.6 \
#           --pcap bonded.pcap
#   Or both at once from Machine 1: ./tx_bonding_e2e.sh bonded 30
#   Compare:
#       tcpdump -r bonded.pcap | wc -l
#       # vs: ./wifi_trx.py --mode tx --samp-rate 20e6 --tx-serial 30DBC3D
#             ./wifi_trx.py --mode rx  --samp-rate 20e6 --rx-serial 30EDB63 \
#                 --pcap baseline.pcap
#
# Requires: gnuradio, gr-ieee802-11, gr-foo, uhd, gr-bonded-usrp
#           external 10 MHz + PPS shared between both bonded TX radios

import faulthandler
import os
import sys
import signal
import argparse
import time

sys.path.append(os.environ.get('GRC_HIER_PATH', os.path.expanduser('~/.grc_gnuradio')))

import pmt
from gnuradio import blocks, gr, network
import ieee802_11
import foo
from gnuradio import bonded_usrp

from wifi_phy_hier import wifi_phy_hier


ENCODING_NAMES = [
    'BPSK 1/2', 'BPSK 3/4', 'QPSK 1/2', 'QPSK 3/4',
    '16QAM 1/2', '16QAM 3/4', '64QAM 2/3', '64QAM 3/4',
]


class WifiBondedTx(gr.top_block):
    """wifi_phy_hier → band_splitter → bonded_sink (all DSP in C++).

    Chain:
      [strobe] → mac ──msg──▶ wifi_phy_hier ──stream──▶ multiply ──▶ pad
                null_src ──────────────────────────────────────────────▲
      pad ──▶ band_splitter ──▶ bonded_sink (radio A / radio B)
    """

    def __init__(self, args):
        gr.top_block.__init__(self, "WiFi Bonded TX", catch_exceptions=True)

        fc           = args.freq
        samp_rate    = args.samp_rate          # combined/wideband rate (e.g. 20 MHz)
        if args.split == 'ofdm':
            # Same per-radio plan as the bonded RX: 5/8 of the channel per
            # radio at fc -/+ 12 subcarriers.
            per_radio = args.per_radio_rate or samp_rate * 5 / 8
            half_bw   = args.radio_offset or samp_rate * 12 / 64
        else:
            per_radio = args.per_radio_rate or samp_rate / 2.0
            half_bw   = args.radio_offset or samp_rate * 14 / 64
        freq_a       = fc - half_bw            # lower radio center
        freq_b       = fc + half_bw            # upper radio center

        print(f"[bonded-tx] fc={fc/1e6:.3f} MHz  combined={samp_rate/1e6:g} MHz  "
              f"per-radio={per_radio/1e6:g} MS/s  radio offset=±{half_bw/1e6:g} MHz  "
              f"split={args.split}")
        print(f"[bonded-tx] Radio A ({args.serial_a}) @ {freq_a/1e6:.3f} MHz")
        print(f"[bonded-tx] Radio B ({args.serial_b}) @ {freq_b/1e6:.3f} MHz")
        print(f"[bonded-tx] delay trim: A={args.delay_trim_a} B={args.delay_trim_b} samples")
        print(f"[bonded-tx] Requires external 10 MHz + PPS on both TX radios")

        # ── 802.11 PHY (operates at the combined wideband rate) ───────────────
        self.phy = wifi_phy_hier(
            bandwidth=samp_rate,
            chan_est=ieee802_11.LS,
            encoding=ieee802_11.Encoding(args.encoding),
            frequency=fc,
            sensitivity=0.56,
        )

        self.mac = ieee802_11.mac(
            args.src_mac, args.dst_mac, args.bss_mac,
        )

        self.strobe = blocks.message_strobe(
            pmt.intern("x" * args.pdu_length), args.interval,
        )

        # wifi_phy_hier needs a null stream input (baseband sense path, unused TX)
        self.null_src = blocks.vector_source_c((0,), True, 1, [])

        # Scale TX amplitude and ensure minimum output buffer for bursty wifi
        self.multiply = blocks.multiply_const_cc(args.tx_amplitude)
        self.multiply.set_min_output_buffer(100000)

        # Zero-pad between packets so the receiver sees clean burst boundaries.
        # packet_pad2(debug, delay, delay_sec, pad_front_samples, pad_tail_samples)
        self.pad = foo.packet_pad2(False, False, 0.01, 100, 1000)
        self.pad.set_min_output_buffer(96000)

        # Optional TCP PDU input
        if args.tcp_port:
            self.socket_pdu = network.socket_pdu(
                'TCP_SERVER', '', str(args.tcp_port), 10000, False)
            self.msg_connect((self.socket_pdu, 'pdus'), (self.mac, 'app in'))

        # ── Split into the two per-radio streams (C++ DSP) ───────────────────
        # ofdm: per-symbol subcarrier split (needs the PHY's packet_len tags at
        # each frame start, so it is fed straight from the PHY, no padding);
        # filter: generic band_splitter.
        if args.split == 'ofdm':
            self.splitter = bonded_usrp.ofdm_splitter(
                samp_rate, per_radio, -half_bw, +half_bw, 64, 16,
                args.split_window, "packet_len")
        else:
            self.splitter = bonded_usrp.band_splitter(
                samp_rate, per_radio, -half_bw, +half_bw, args.split_taps)

        # ── Synchronized 2-radio TX sink (C++ bonded_transmitter) ─────────────
        freq_plan_csv = (args.freq_plan_csv
                         if args.freq_plan_csv
                         else f"{freq_a},{freq_b}")
        # Per-radio TX gain to balance the two half-bands at the receiver
        # (two independent B210s/antennas rarely radiate equal power).  Falls
        # back to the uniform --tx-gain when a per-radio value is unset.
        gain_a = args.tx_gain_a if args.tx_gain_a is not None else args.tx_gain
        gain_b = args.tx_gain_b if args.tx_gain_b is not None else args.tx_gain
        gain_plan_csv = (f"{gain_a},{gain_b}"
                         if (args.tx_gain_a is not None
                             or args.tx_gain_b is not None)
                         else "")
        if gain_plan_csv:
            print(f"[bonded-tx] per-radio TX gain: A={gain_a} B={gain_b}")

        # Inter-radio phase-coherence correction (applied to radio B).
        # freeze the relative CFO with --freq-offset-b and pin the relative
        # phase with --phase-offset-b (or live through --phase-ctl-port).
        freq_offset_csv  = f"{args.freq_offset_a},{args.freq_offset_b}"
        phase_offset_csv = f"{args.phase_offset_a},{args.phase_offset_b}"
        if args.freq_offset_b or args.phase_offset_b or args.freq_offset_a or args.phase_offset_a:
            print(f"[bonded-tx] phase corr: A(f={args.freq_offset_a}Hz,ph={args.phase_offset_a}) "
                  f"B(f={args.freq_offset_b}Hz,ph={args.phase_offset_b})")

        self.bonded_snk = bonded_usrp.bonded_sink(
            f"{args.serial_a},{args.serial_b}",
            per_radio,
            fc,
            args.tx_gain,
            args.clock_source,
            args.time_source,
            args.strict_lock,
            args.lock_timeout,
            "A:B",          # subdev: VERT2450 antenna port on B210
            freq_plan_csv,
            args.stream_args,
            args.delay_trim_a,
            args.delay_trim_b,
            gain_plan_csv,
            freq_offset_csv,
            phase_offset_csv,
        )

        # ── Connections ───────────────────────────────────────────────────────
        # PHY message path
        self.msg_connect((self.strobe, 'strobe'), (self.mac, 'app in'))
        self.msg_connect((self.mac, 'phy out'), (self.phy, 'mac_in'))

        # PHY stream path → split → transmit
        self.connect((self.null_src, 0),   (self.phy,      0))
        self.connect((self.phy,      0),   (self.multiply, 0))
        if args.split == 'ofdm':
            self.connect((self.multiply, 0), (self.splitter, 0))
        else:
            self.connect((self.multiply, 0), (self.pad,      0))
            self.connect((self.pad,      0), (self.splitter, 0))
        self.connect((self.splitter, 0),   (self.bonded_snk, 0))  # radio A
        self.connect((self.splitter, 1),   (self.bonded_snk, 1))  # radio B


def parse_mac_addr(s):
    parts = s.split(':')
    if len(parts) != 6:
        raise argparse.ArgumentTypeError(
            f"MAC address must be xx:xx:xx:xx:xx:xx, got: {s}")
    return [int(p, 16) for p in parts]


def main():
    p = argparse.ArgumentParser(
        description='Bonded WiFi TX — hard-split wideband across two B210s '
                    '(all DSP in C++).',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    # ── Radio parameters ──────────────────────────────────────────────────────
    p.add_argument('--freq', type=float, default=2.484e9,
                   help='Channel center fc in Hz (must match the RX). '
                        'Default 2.484e9 = 802.11 channel 14, kept clear of '
                        'the polluted 1/6/11 production WiFi channels.')
    p.add_argument('--samp-rate', type=float, default=20e6,
                   help='Combined wideband rate in Hz; per-radio = samp-rate/2')
    p.add_argument('--per-radio-rate', type=float, default=None,
                   help='Per-radio TX rate in Hz (default 5/8*samp-rate with '
                        '--split ofdm, samp-rate/2 with --split filter).')
    p.add_argument('--radio-offset', type=float, default=None,
                   help='Each radio is tuned to fc -/+ this (Hz; default '
                        '12/64*samp-rate with --split ofdm, a whole number of '
                        'subcarriers; 14/64*samp-rate with --split filter, '
                        'which keeps the seam off the radio Nyquist edge).')
    p.add_argument('--tx-gain', type=float, default=0.5,
                   help='Uniform TX gain, normalized [0.0, 1.0]; 0.5 ≈ 44 dB '
                        'on B210.  Used for any radio without a per-radio override.')
    p.add_argument('--tx-gain-a', type=float, default=None,
                   help='Per-radio normalized TX gain for Radio A (lower half); '
                        'overrides --tx-gain.  Use to balance the two half-bands '
                        'at the RX (e.g. boost the weaker radio).')
    p.add_argument('--tx-gain-b', type=float, default=None,
                   help='Per-radio normalized TX gain for Radio B (upper half); '
                        'overrides --tx-gain.')

    # ── Inter-radio phase-coherence correction ────────────────────────────────
    p.add_argument('--freq-offset-a', type=float, default=0.0,
                   help='Digital freq correction (Hz) on Radio A (rarely needed).')
    p.add_argument('--freq-offset-b', type=float, default=0.0,
                   help='Digital freq correction (Hz) on Radio B to freeze the '
                        'inter-radio relative CFO (set to -measured_CFO).')
    p.add_argument('--phase-offset-a', type=float, default=0.0,
                   help='Static phase offset (rad) on Radio A.')
    p.add_argument('--phase-offset-b', type=float, default=0.0,
                   help='Static phase offset (rad) on Radio B to pin the '
                        'inter-radio relative phase away from the sync dead zone.')
    p.add_argument('--split', choices=('ofdm', 'filter'), default='ofdm',
                   help='ofdm: split every OFDM symbol by subcarrier and '
                        'synthesize each radio at its own rate (ofdm_splitter; '
                        'insensitive to the radios\' relative phase). filter: '
                        'band_splitter, a generic split filter at the seam.')
    p.add_argument('--split-window', type=int, default=8,
                   help='ofdm split: raised-cosine overlap between symbols, in '
                        'samples at --samp-rate (<= 16, the 802.11 CP)')
    p.add_argument('--split-taps', type=int, default=64,
                   help='filter split: taps per polyphase phase (filter length '
                        '= taps*2+1 for the 2x split).  Sets how many '
                        'subcarriers around the seam both radios radiate; they '
                        'are not phase-coherent, so those subcarriers can fade.')
    p.add_argument('--phase-ctl-port', type=int, default=None,
                   help='UDP port for live phase correction ("B <rad>" = set, '
                        '"B add <rad>" = add); driven by tx_phase_cal.py')
    p.add_argument('--tx-amplitude', type=float, default=0.4,
                   help='Baseband amplitude scale applied before the splitter. '
                        '0.4 keeps each radio below DAC full scale (0.6 clips '
                        'OFDM peaks and costs 64-QAM); use --tx-gain for power.')
    p.add_argument('--serial-a', type=str, default='30B56D6',
                   help='B210 serial for Radio A (lower half-band)')
    p.add_argument('--serial-b', type=str, default='30DBC3C',
                   help='B210 serial for Radio B (upper half-band)')

    # ── Sync / hardware ───────────────────────────────────────────────────────
    p.add_argument('--clock-source', type=str, default='external',
                   help='UHD clock source (external, internal, gpsdo)')
    p.add_argument('--time-source', type=str, default='external',
                   help='UHD time source (external, internal, gpsdo)')
    p.add_argument('--strict-lock', action='store_true',
                   help='Throw on reference lock failure instead of warning')
    p.add_argument('--lock-timeout', type=float, default=5.0,
                   help='Seconds to wait for 10 MHz reference lock')
    p.add_argument('--stream-args', type=str, default='',
                   help='Extra UHD TX stream args')
    p.add_argument('--freq-plan-csv', type=str, default='',
                   help='Override per-device center freqs (Hz, comma-separated).'
                        '  Default: fc ± radio-offset')

    # ── Per-device delay trim ─────────────────────────────────────────────────
    p.add_argument('--delay-trim-a', type=int, default=0,
                   help='Integer sample-delay trim for Radio A (positive = '
                        'delay A by N samples at per-radio rate).  Use to '
                        'correct residual inter-device skew after PPS alignment.')
    p.add_argument('--delay-trim-b', type=int, default=0,
                   help='Integer sample-delay trim for Radio B.')

    # ── 802.11 MAC/PHY parameters ─────────────────────────────────────────────
    p.add_argument('--encoding', type=int, default=0, choices=range(8),
                   help='MCS index: '
                        + ', '.join(f'{i}={n}'
                                    for i, n in enumerate(ENCODING_NAMES)))
    p.add_argument('--pdu-length', type=int, default=500,
                   help='TX payload size in bytes')
    p.add_argument('--interval', type=int, default=300,
                   help='TX strobe period in ms')
    p.add_argument('--tcp-port', type=int, default=None,
                   help='TCP server port for external PDU input')
    p.add_argument('--src-mac', type=parse_mac_addr,
                   default='23:23:23:23:23:23',
                   help='TX source MAC address')
    p.add_argument('--dst-mac', type=parse_mac_addr,
                   default='42:42:42:42:42:42',
                   help='TX destination MAC address')
    p.add_argument('--bss-mac', type=parse_mac_addr,
                   default='ff:ff:ff:ff:ff:ff',
                   help='TX BSS MAC address')

    # ── GR scheduler ─────────────────────────────────────────────────────────
    p.add_argument('--max-noutput-items', type=int, default=8192,
                   help='GNU Radio scheduler max noutput_items')

    args = p.parse_args()

    print(f"[bonded-tx] WiFi bonded TX  fc={args.freq/1e6:.3f} MHz  "
          f"combined={args.samp_rate/1e6:g} MHz  "
          f"encoding={ENCODING_NAMES[args.encoding]}  interval={args.interval} ms")

    faulthandler.enable()  # Python stacks of every thread if the process aborts
    tb = WifiBondedTx(args)

    # Sent-frame counter, same format as packet_rate_counter.py on the RX so
    # the two can be compared side by side. Counts frames actually handed to
    # the radios (bonded_transmitter), timed from the first one.
    first_ts = None

    def tx_report(n, final=False):
        if first_ts is None:
            if final:
                print("[TX FINAL] no frames sent", flush=True)
            return
        elapsed = time.monotonic() - first_ts
        rate = f"{n / elapsed * 60.0:7.2f}" if elapsed >= 1.0 else "    n/a"
        tag = "TX FINAL" if final else "txcount"
        print(f"[{tag}] total={n}  elapsed={elapsed:7.1f}s  rate={rate} pkt/min",
              flush=True)

    # Only stop here; the end of main() reports. Exiting from the handler
    # would finalize Python while GR's waiter thread is still leaving C++,
    # which now and then aborts the process (std::terminate).
    stopping = False

    def shutdown(sig=None, frame=None):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        print(f"\n[bonded-tx] shutting down (signal {sig}) ...", flush=True)
        tb.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    tb.start(args.max_noutput_items)
    print("[bonded-tx] running — press Ctrl+C to stop")

    def tx_count():
        nonlocal first_ts
        last = 0
        while True:
            n = tb.bonded_snk.frames_sent()
            if n != last:
                if first_ts is None:
                    first_ts = time.monotonic()
                last = n
                tx_report(n)
            time.sleep(0.02)

    import threading
    threading.Thread(target=tx_count, daemon=True).start()

    if args.phase_ctl_port:
        # Receiver feedback (see tx_phase_cal.py): datagram "B <rad>" sets
        # radio B's phase rotation, "B add <rad>" adds to it.  Control only —
        # the rotation itself is applied in C++ by bonded_transmitter.
        import math
        import socket

        def phase_ctl():
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sk.bind(('0.0.0.0', args.phase_ctl_port))
            while True:
                msg = sk.recv(256).decode(errors='replace').split()
                if len(msg) not in (2, 3) or msg[0] not in ('A', 'B'):
                    continue
                if len(msg) == 3 and msg[1] != 'add':
                    continue
                dev = 0 if msg[0] == 'A' else 1
                try:
                    val = float(msg[-1])
                except ValueError:
                    continue
                new = tb.bonded_snk.phase_offset(dev) + val if len(msg) == 3 else val
                new = math.remainder(new, 2 * math.pi)
                tb.bonded_snk.set_phase_offset(dev, new)
                print(f"[bonded-tx] phase {msg[0]} = {new:+.3f} rad", flush=True)

        threading.Thread(target=phase_ctl, daemon=True).start()
        print(f"[bonded-tx] phase control on udp/{args.phase_ctl_port}")

    tb.wait()
    tx_report(tb.bonded_snk.frames_sent(), final=True)


if __name__ == '__main__':
    main()
