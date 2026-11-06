#!/usr/bin/env python3
#
# Bonded WiFi RX — overlap-aware wideband reconstruction.
#
# Thesis design (intentional frequency overlap, NOT a hard split)
# ---------------------------------------------------------------
# A 20 MHz 802.11a/g channel (from wifi_trx.py --samp-rate 20e6) centered at fc
# spans [fc-10, fc+10] MHz.  Two B210s each capture a *wider* 15 MHz slice with
# a 5 MHz shared overlap:
#
#   Radio A  serial_a  tuned to fc-5 MHz  at 15 Msps  -> [fc-12.5, fc+2.5] MHz
#   Radio B  serial_b  tuned to fc+5 MHz  at 15 Msps  -> [fc-2.5, fc+12.5] MHz
#   overlap: [fc-2.5, fc+2.5] MHz  (5 MHz shared)
#
# ALL reconstruction DSP lives in C++:
#   - bonded_usrp.bonded_source        synchronized 2-radio acquisition
#                                       (PPS/MCR alignment in bonded_receiver)
#   - bonded_usrp.overlap_reconstructor align the overlap (gain/phase/delay),
#                                       crossfade the shared region, discard the
#                                       out-of-band corners, output one 20 Msps
#                                       contiguous baseband centered at fc.
#
# This Python file is only a thin flowgraph: source -> reconstructor ->
# standard gr-ieee802-11 PHY -> frame counter.  No DSP is done in Python.
#
# Usage
# -----
#   Terminal 1 — TX (20 MHz):
#       ./wifi_trx.py --mode tx --samp-rate 20e6 --tx-serial 30DBC3D
#   Terminal 2 — bonded RX (this script):
#       ./wifi_bonded_rx.py --serial-a 30DBC3D --serial-b 30EDB63 --rx-gain-db 30
#
# Requires: gnuradio, gr-ieee802-11, gr-foo, uhd, gr-bonded-usrp
#           external 10 MHz + PPS shared between both bonded radios

import sys
import signal
import argparse

import pmt
from gnuradio import blocks, fft, gr
from gnuradio.fft import window
import ieee802_11
import foo
from gnuradio import bonded_usrp

CHAN_EST_NAMES = ['LS', 'LMS', 'Linear Comb', 'STA']


class WifiBondedRx(gr.top_block):
    """source -> overlap_reconstructor -> gr-ieee802-11 PHY (all DSP in C++).

    Modes:
    live    : bonded_source -> reconstructor -> PHY (real-time; CPU-heavy)
      record  : bonded_source -> two raw fc32 files (lightweight; for offline use)
      replay  : two raw fc32 files -> reconstructor -> PHY (lossless, slower
                than real-time but no overflow — the recommended validation path)
    """

    def __init__(self, args):
        gr.top_block.__init__(self, "WiFi Bonded RX", catch_exceptions=True)

        fc        = args.freq
        samp_rate = args.samp_rate            # combined / target bandwidth (T)
        # Edge-aligned plan: radio A covers exactly [fc-T/2, fc+O/2] (channel's
        # lower edge up to the overlap's upper edge) and radio B its mirror, so
        # the union is exactly the target channel with NO captured-then-discarded
        # corner. That makes per-radio capture T/2 + O/2 and puts the radios at
        # fc ∓ (T-O)/4.
        #   T=20, O=5: per-radio 12.5 MHz, radios at fc∓3.75  (was 15 MHz, fc∓5)
        #   T=40, O=10: per-radio 25 MHz,  radios at fc∓7.5   (was 30 MHz, fc∓10)
        # Besides saving 17% of per-radio USB bandwidth, the resulting resample
        # ratio (8/5) needs fewer taps per polyphase phase than the old 4/3, so
        # the reconstructor is ~9% faster per output sample as well.
        # Use --per-radio-rate to override (e.g. T/2+O reproduces the old plan).
        overlap   = args.overlap if args.overlap else 0.25 * samp_rate
        per_radio = (args.per_radio_rate if args.per_radio_rate
                     else 0.5 * samp_rate + 0.5 * overlap)
        off       = 0.5 * (per_radio - overlap)   # = (T-O)/4 for the default plan
        freq_a    = fc - off
        freq_b    = fc + off
        # Spectrum each radio captures beyond the target channel edge (0 for the
        # default edge-aligned plan; positive if --per-radio-rate is widened).
        corner    = 0.5 * per_radio + off - 0.5 * samp_rate
        print(f"[bonded] plan: per-radio={per_radio/1e6:.3f} MHz  "
              f"overlap={overlap/1e6:.3f} MHz  corner-discard/radio="
              f"{max(corner, 0.0)/1e6:.3f} MHz  resample={samp_rate/1e6:.0f}/"
              f"{per_radio/1e6:.3f}")
        self.mode = args.mode

        print(f"[bonded] mode={args.mode}  target={samp_rate/1e6:.0f} MHz @ "
              f"{fc/1e6:.3f} MHz  per-radio={per_radio/1e6:.0f} MHz  "
              f"overlap={overlap/1e6:.0f} MHz")
        print(f"[bonded] Radio A ({args.serial_a}) @ {freq_a/1e6:.3f} MHz  "
              f"-> [{(freq_a-per_radio/2)/1e6:.1f}, {(freq_a+per_radio/2)/1e6:.1f}] MHz")
        print(f"[bonded] Radio B ({args.serial_b}) @ {freq_b/1e6:.3f} MHz  "
              f"-> [{(freq_b-per_radio/2)/1e6:.1f}, {(freq_b+per_radio/2)/1e6:.1f}] MHz")

        rec_a = f"{args.rec_prefix}_a.fc32"
        rec_b = f"{args.rec_prefix}_b.fc32"

        if args.mode in ("live", "record"):
            # ── Synchronized acquisition (C++ bonded_receiver) ────────────────
            # subdev "A:B" -> output ports 0,1,2,3 = dev0ch0, dev0ch1, dev1ch0,
            # dev1ch1.  We use ch0 of each device (ports 0 and 2).
            freq_plan_csv = f"{freq_a},{freq_b}"
            self.bonded_src = bonded_usrp.bonded_source(
                f"{args.serial_a},{args.serial_b}",
                per_radio, fc, args.rx_gain_db,
                "external", "external", False, 5.0,
                args.fetch_timeout, "A:B", freq_plan_csv, args.stream_args)
            self.null_a = blocks.null_sink(gr.sizeof_gr_complex)
            self.null_b = blocks.null_sink(gr.sizeof_gr_complex)
            self.connect((self.bonded_src, 1), (self.null_a, 0))
            self.connect((self.bonded_src, 3), (self.null_b, 0))

        if args.mode == "record":
            # Lightweight: dump the two synchronized slices to disk, no DSP.
            self.fsink_a = blocks.file_sink(gr.sizeof_gr_complex, rec_a, False)
            self.fsink_b = blocks.file_sink(gr.sizeof_gr_complex, rec_b, False)
            self.fsink_a.set_unbuffered(False)
            self.fsink_b.set_unbuffered(False)
            self.connect((self.bonded_src, 0), (self.fsink_a, 0))
            self.connect((self.bonded_src, 2), (self.fsink_b, 0))
            print(f"[bonded] recording slices -> {rec_a} , {rec_b}")
            return

        # ── Overlap-aware reconstruction (C++ overlap_reconstructor) ──────────
        self.recon = bonded_usrp.overlap_reconstructor(
            per_radio, samp_rate, samp_rate, overlap, -off, off)

        if args.mode == "live":
            self.connect((self.bonded_src, 0), (self.recon, 0))  # dev0 ch0
            self.connect((self.bonded_src, 2), (self.recon, 1))  # dev1 ch0
        else:  # replay
            self.fsrc_a = blocks.file_source(gr.sizeof_gr_complex, rec_a, False)
            self.fsrc_b = blocks.file_source(gr.sizeof_gr_complex, rec_b, False)
            self.connect((self.fsrc_a, 0), (self.recon, 0))
            self.connect((self.fsrc_b, 0), (self.recon, 1))
            print(f"[bonded] replaying slices from {rec_a} , {rec_b}")

        if args.iq_capture:
            self.iq_sink = blocks.file_sink(gr.sizeof_gr_complex, args.iq_capture, False)
            self.iq_sink.set_unbuffered(False)
            self.connect((self.recon, 0), (self.iq_sink, 0))

        if args.no_decode:
            self.null_recon = blocks.null_sink(gr.sizeof_gr_complex)
            self.connect((self.recon, 0), (self.null_recon, 0))
            print("[bonded] decode disabled; reconstructing only")
            return

        # ── Standard gr-ieee802-11 receive chain at the combined rate ─────────
        self._build_phy(fc, samp_rate, args.chan_est, args.pcap, args.phy_frontend)
        if args.phy_frontend == 'fused':
            self.connect((self.recon, 0), (self.sync_short, 0))
        else:
            self.connect((self.recon, 0), (self.delay_sc, 0))
            self.connect((self.recon, 0), (self.mult_sc, 0))
            self.connect((self.recon, 0), (self.c2mag_sq, 0))

    def _build_phy(self, freq, samp_rate, chan_est, pcap, frontend='blocks'):
        # 802.11 preamble is a fixed 320 samples regardless of sample rate.
        delay_sc, window_size, sync_length = 16, 48, 320

        if frontend == 'fused':
            # sync_short with the Schmidl-Cox chain below built in (one block
            # instead of nine); needs the patched gr-ieee802-11.
            self.sync_short = ieee802_11.sync_short_fused(0.56, 2, False, False)
        else:
            self._build_schmidl_cox(delay_sc, window_size)
        self.delay_sync = blocks.delay(gr.sizeof_gr_complex, sync_length)
        self.sync_long  = ieee802_11.sync_long(sync_length, False, False)
        self.s2v        = blocks.stream_to_vector(gr.sizeof_gr_complex, 64)
        self.ofdm_fft   = fft.fft_vcc(64, True, window.rectangular(64), True, 1)
        self.equalizer  = ieee802_11.frame_equalizer(
            ieee802_11.Equalizer(chan_est), freq, samp_rate, False, False)
        self.decode_mac = ieee802_11.decode_mac(True, False)
        self.parse_mac  = ieee802_11.parse_mac(True, False)

        if pcap:
            self.wireshark = foo.wireshark_connector(foo.WIFI, False)
            self.file_sink = blocks.file_sink(gr.sizeof_char, pcap, True)
            self.file_sink.set_unbuffered(True)
            self.msg_connect((self.decode_mac, 'out'), (self.wireshark, 'in'))
            self.connect((self.wireshark, 0), (self.file_sink, 0))
        self.msg_connect((self.decode_mac, 'out'), (self.parse_mac, 'in'))

        # Long-preamble sync -> OFDM demod -> equalize -> decode.
        self.connect((self.sync_short, 0), (self.delay_sync, 0))
        self.connect((self.sync_short, 0), (self.sync_long, 0))
        self.connect((self.delay_sync, 0), (self.sync_long, 1))
        self.connect((self.sync_long, 0),  (self.s2v, 0))
        self.connect((self.s2v, 0),        (self.ofdm_fft, 0))
        self.connect((self.ofdm_fft, 0),   (self.equalizer, 0))
        self.connect((self.equalizer, 0),  (self.decode_mac, 0))

    def _build_schmidl_cox(self, delay_sc, window_size):
        # Schmidl-Cox short-preamble autocorrelation + normalization.
        self.delay_sc   = blocks.delay(gr.sizeof_gr_complex, delay_sc)
        self.conj       = blocks.conjugate_cc()
        self.mult_sc    = blocks.multiply_vcc(1)
        self.ma_cc      = blocks.moving_average_cc(window_size, 1, 4000, 1)
        self.ma_ff      = blocks.moving_average_ff(window_size + 16, 1, 4000, 1)
        self.c2mag      = blocks.complex_to_mag(1)
        self.c2mag_sq   = blocks.complex_to_mag_squared(1)
        self.divide     = blocks.divide_ff(1)
        self.sync_short = ieee802_11.sync_short(0.56, 2, False, False)

        self.connect((self.delay_sc, 0), (self.conj, 0))
        self.connect((self.delay_sc, 0), (self.sync_short, 0))
        self.connect((self.conj, 0),     (self.mult_sc, 1))
        self.connect((self.mult_sc, 0),  (self.ma_cc, 0))
        self.connect((self.ma_cc, 0),    (self.c2mag, 0))
        self.connect((self.ma_cc, 0),    (self.sync_short, 1))
        self.connect((self.c2mag_sq, 0), (self.ma_ff, 0))
        self.connect((self.c2mag, 0),    (self.divide, 0))
        self.connect((self.ma_ff, 0),    (self.divide, 1))
        self.connect((self.divide, 0),   (self.sync_short, 2))


def main():
    p = argparse.ArgumentParser(
        description='Bonded WiFi RX — overlap-aware reconstruction (DSP in C++).',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--mode', choices=('live', 'record', 'replay'), default='live',
                   help='live: real-time decode (CPU-heavy); record: dump slices '
                        'to disk; replay: reconstruct+decode from disk (lossless)')
    p.add_argument('--rec-prefix', type=str, default='bonded_slice',
                   help='record/replay file prefix (writes/reads PREFIX_a.fc32, '
                        'PREFIX_b.fc32)')
    p.add_argument('--freq', type=float, default=2.412e9,
                   help='Channel center fc in Hz (match TX)')
    p.add_argument('--samp-rate', type=float, default=20e6,
                   help='Combined / target rate in Hz (the 802.11 channel width)')
    p.add_argument('--overlap', type=float, default=None,
                   help='Shared overlap bandwidth in Hz (default 0.25*samp-rate). '
                        'Per-radio capture becomes samp-rate/2 + overlap; a '
                        'smaller overlap lowers per-radio USB bandwidth but '
                        'needs sharper (costlier) reconstruction filters.')
    p.add_argument('--per-radio-rate', type=float, default=None,
                   help='Override per-radio slice rate in Hz '
                        '(default samp-rate/2 + overlap)')
    p.add_argument('--chan-est', type=int, default=0, choices=range(4),
                   help='Channel estimator: ' +
                        ', '.join(f'{i}={n}' for i, n in enumerate(CHAN_EST_NAMES)))
    p.add_argument('--phy-frontend', choices=('auto', 'blocks', 'fused'), default='auto',
                   help='Schmidl-Cox front-end: stock GR blocks, or one '
                        'ieee802_11.sync_short_fused (patched gr-ieee802-11, '
                        'needed for decode above 60 MHz); auto = fused when '
                        'the loaded gr-ieee802-11 has it')
    p.add_argument('--pcap', type=str, default=None,
                   help='Append decoded frames to this pcap file')
    p.add_argument('--iq-capture', type=str, default=None,
                   help='Write reconstructed IQ (complex64/fc32) to this file')
    p.add_argument('--no-decode', action='store_true',
                   help='Run bonded source + reconstructor only, skip 802.11 decode')
    p.add_argument('--serial-a', type=str, default='30B56D6',
                   help='USRP serial for Radio A (lower slice)')
    p.add_argument('--serial-b', type=str, default='30DBC3C',
                   help='USRP serial for Radio B (upper slice)')
    p.add_argument('--rx-gain-db', type=float, default=35.0, help='RX gain in dB')
    p.add_argument('--fetch-timeout', type=float, default=1.0,
                   help='bonded receiver fetch timeout per chunk (seconds)')
    p.add_argument('--stream-args', type=str,
                   default='num_recv_frames=512,recv_frame_size=8192',
                   help='extra UHD stream args for bonded source')
    p.add_argument('--max-noutput-items', type=int, default=8192,
                   help='GNU Radio scheduler max noutput_items')
    args = p.parse_args()
    if args.phy_frontend == 'auto':
        args.phy_frontend = 'fused' if hasattr(ieee802_11, 'sync_short_fused') else 'blocks'

    print(f"[bonded] WiFi bonded RX  fc={args.freq/1e6:.3f} MHz  "
          f"combined={args.samp_rate/1e6:.0f} Msps  rx_gain={args.rx_gain_db:.1f} dB  "
          f"phy_frontend={args.phy_frontend}")
    print(f"[bonded] Requires external 10 MHz + PPS on both radios")

    tb = WifiBondedRx(args)

    if args.mode == 'replay':
        # Batch: process the whole capture, then stop.
        print("[bonded] replaying (lossless, slower than real-time) ...")
        tb.run()
        print("[bonded] replay complete")
        return

    def shutdown(sig=None, frame=None):
        print("\n[bonded] shutting down ...")
        tb.stop()
        tb.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    tb.start(args.max_noutput_items)
    print("[bonded] running — press Ctrl+C to stop")
    tb.wait()


if __name__ == '__main__':
    main()
