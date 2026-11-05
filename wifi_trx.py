#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0
#
# Headless IEEE 802.11 WiFi TX/RX - no Qt GUI, driven by argparse.
#
# Requires: gnuradio runtime, gr-ieee802-11, gr-foo, uhd
#
# TX serial: 30DBC3D (A:B)  RX serial: 30EDB63 (A:B)
#
# Usage examples:
#   ./wifi_trx.py --mode both
#   ./wifi_trx.py --mode tx --encoding 2 --interval 500
#   ./wifi_trx.py --mode rx --freq 5.18e9 --pcap /tmp/wifi.pcap
#   ./wifi_trx.py --mode tx --tcp-port 52001  # accept PDUs over TCP

import os
import sys
import signal
import argparse
import time

sys.path.append(os.environ.get('GRC_HIER_PATH', os.path.expanduser('~/.grc_gnuradio')))

import pmt
from gnuradio import blocks, fft, gr, network, uhd
from gnuradio.fft import window
import ieee802_11
import foo

from wifi_phy_hier import wifi_phy_hier


ENCODING_NAMES = [
    'BPSK 1/2', 'BPSK 3/4', 'QPSK 1/2', 'QPSK 3/4',
    '16QAM 1/2', '16QAM 3/4', '64QAM 2/3', '64QAM 3/4',
]
CHAN_EST_NAMES = ['LS', 'LMS', 'Linear Comb', 'STA']


class WifiTx(gr.top_block):
    def __init__(self, args):
        gr.top_block.__init__(self, "WiFi TX", catch_exceptions=True)

        freq       = args.freq
        samp_rate  = args.samp_rate
        lo_offset  = args.lo_offset
        tx_gain    = args.tx_gain
        encoding   = args.encoding
        pdu_length = args.pdu_length
        interval   = args.interval

        self.phy = wifi_phy_hier(
            bandwidth=samp_rate,
            chan_est=ieee802_11.LS,
            encoding=ieee802_11.Encoding(encoding),
            frequency=freq,
            sensitivity=0.56,
        )

        self.usrp_sink = uhd.usrp_sink(
            ",".join(('', f"serial={args.tx_serial}")),
            uhd.stream_args(cpu_format="fc32", args='', channels=[0]),
            'packet_len',
        )
        self.usrp_sink.set_subdev_spec('A:B', 0)
        self.usrp_sink.set_samp_rate(samp_rate)
        self.usrp_sink.set_time_now(uhd.time_spec(time.time()), uhd.ALL_MBOARDS)
        self.usrp_sink.set_center_freq(
            uhd.tune_request(freq, rf_freq=freq - lo_offset,
                             rf_freq_policy=uhd.tune_request.POLICY_MANUAL), 0)
        self.usrp_sink.set_normalized_gain(tx_gain, 0)

        self.mac = ieee802_11.mac(
            args.src_mac, args.dst_mac, args.bss_mac,
        )

        self.strobe = blocks.message_strobe(
            pmt.intern("x" * pdu_length), interval,
        )

        self.multiply = blocks.multiply_const_cc(0.6)
        self.multiply.set_min_output_buffer(100000)

        self.pad = foo.packet_pad2(False, False, 0.01, 100, 1000)
        self.pad.set_min_output_buffer(96000)

        # wifi_phy_hier needs a stream input (fed zeros when not TX-ing)
        self.null_src = blocks.vector_source_c((0,), False, 1, [])

        # Optional: accept PDUs from a TCP client
        if args.tcp_port:
            self.socket_pdu = network.socket_pdu(
                'TCP_SERVER', '', str(args.tcp_port), 10000, False,
            )
            self.msg_connect((self.socket_pdu, 'pdus'), (self.mac, 'app in'))

        self.msg_connect((self.strobe, 'strobe'), (self.mac, 'app in'))
        self.msg_connect((self.mac, 'phy out'), (self.phy, 'mac_in'))
        self.connect((self.null_src, 0), (self.phy, 0))
        self.connect((self.phy, 0), (self.multiply, 0))
        self.connect((self.multiply, 0), (self.pad, 0))
        self.connect((self.pad, 0), (self.usrp_sink, 0))


class WifiRx(gr.top_block):
    def __init__(self, args):
        gr.top_block.__init__(self, "WiFi RX", catch_exceptions=True)

        freq        = args.freq
        samp_rate   = args.samp_rate
        lo_offset   = args.lo_offset
        rx_gain     = args.rx_gain
        chan_est    = args.chan_est
        window_size = 48
        sync_length = 320

        self.usrp_src = uhd.usrp_source(
            ",".join(('', f"serial={args.rx_serial}",
                      "num_recv_frames=512", "recv_frame_size=8192")),
            uhd.stream_args(cpu_format="fc32", args='', channels=[0]),
        )
        self.usrp_src.set_subdev_spec('A:B', 0)
        self.usrp_src.set_samp_rate(samp_rate)
        self.usrp_src.set_time_unknown_pps(uhd.time_spec(0))
        self.usrp_src.set_center_freq(
            uhd.tune_request(freq, rf_freq=freq - lo_offset,
                             rf_freq_policy=uhd.tune_request.POLICY_MANUAL), 0)
        self.usrp_src.set_normalized_gain(rx_gain, 0)

        # Schmidl-Cox preamble detection
        self.delay_16   = blocks.delay(gr.sizeof_gr_complex, 16)
        self.delay_sync = blocks.delay(gr.sizeof_gr_complex, sync_length)
        self.conj       = blocks.conjugate_cc()
        self.mult       = blocks.multiply_vcc(1)
        # Complex moving average of cross-correlation (window_size samples)
        self.ma_cc      = blocks.moving_average_cc(window_size, 1, 4000, 1)
        # Float moving average of power (window_size + 16 samples)
        self.ma_ff      = blocks.moving_average_ff(window_size + 16, 1, 4000, 1)
        self.c2mag      = blocks.complex_to_mag(1)
        self.c2mag_sq   = blocks.complex_to_mag_squared(1)
        self.divide     = blocks.divide_ff(1)

        self.sync_short = ieee802_11.sync_short(0.56, 2, False, False)
        self.sync_long  = ieee802_11.sync_long(sync_length, False, False)
        self.s2v        = blocks.stream_to_vector(gr.sizeof_gr_complex, 64)
        self.ofdm_fft   = fft.fft_vcc(64, True, window.rectangular(64), True, 1)
        self.equalizer  = ieee802_11.frame_equalizer(
            ieee802_11.Equalizer(chan_est), freq, samp_rate, False, False,
        )
        self.decode_mac = ieee802_11.decode_mac(True, False)
        self.parse_mac  = ieee802_11.parse_mac(True, False)

        # Optional raw-IQ capture straight off the USRP (for spectrum/seam
        # diagnosis of the recombined bonded signal).  complex64 (fc32).
        if getattr(args, 'iq_capture', None):
            self.iq_sink = blocks.file_sink(
                gr.sizeof_gr_complex, args.iq_capture, False)
            self.connect((self.usrp_src, 0), (self.iq_sink, 0))

        if args.pcap:
            self.wireshark = foo.wireshark_connector(foo.WIFI, False)
            self.file_sink = blocks.file_sink(gr.sizeof_char, args.pcap, True)
            self.file_sink.set_unbuffered(True)
            self.msg_connect((self.decode_mac, 'out'), (self.wireshark, 'in'))
            self.connect((self.wireshark, 0), (self.file_sink, 0))

        self.msg_connect((self.decode_mac, 'out'), (self.parse_mac, 'in'))

        # x[n] * conj(x[n-16]) cross-correlation (Schmidl-Cox short preamble)
        self.connect((self.usrp_src, 0), (self.mult, 0))
        self.connect((self.usrp_src, 0), (self.delay_16, 0))
        self.connect((self.usrp_src, 0), (self.c2mag_sq, 0))
        self.connect((self.delay_16, 0), (self.conj, 0))
        self.connect((self.delay_16, 0), (self.sync_short, 0))
        self.connect((self.conj, 0), (self.mult, 1))
        self.connect((self.mult, 0), (self.ma_cc, 0))
        self.connect((self.ma_cc, 0), (self.c2mag, 0))
        self.connect((self.ma_cc, 0), (self.sync_short, 1))
        # Normalize correlation by signal power
        self.connect((self.c2mag_sq, 0), (self.ma_ff, 0))
        self.connect((self.c2mag, 0), (self.divide, 0))
        self.connect((self.ma_ff, 0), (self.divide, 1))
        self.connect((self.divide, 0), (self.sync_short, 2))
        # Long preamble sync → OFDM demod
        self.connect((self.sync_short, 0), (self.delay_sync, 0))
        self.connect((self.sync_short, 0), (self.sync_long, 0))
        self.connect((self.delay_sync, 0), (self.sync_long, 1))
        self.connect((self.sync_long, 0), (self.s2v, 0))
        self.connect((self.s2v, 0), (self.ofdm_fft, 0))
        self.connect((self.ofdm_fft, 0), (self.equalizer, 0))
        self.connect((self.equalizer, 0), (self.decode_mac, 0))


def parse_mac_addr(s):
    parts = s.split(':')
    if len(parts) != 6:
        raise argparse.ArgumentTypeError(f"MAC address must be xx:xx:xx:xx:xx:xx, got: {s}")
    return [int(p, 16) for p in parts]


def main():
    parser = argparse.ArgumentParser(
        description='Headless IEEE 802.11 WiFi TX/RX (no Qt GUI)',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument('--mode', choices=['tx', 'rx', 'both'], default='both',
                        help='Operating mode')
    parser.add_argument('--freq', type=float, default=2.484e9,
                        help='Center frequency in Hz (default 2.484e9 = '
                             'channel 14, clear of production WiFi)')
    parser.add_argument('--samp-rate', type=float, default=10e6,
                        help='Sample rate in Hz (5e6, 10e6, or 20e6)')
    parser.add_argument('--lo-offset', type=float, default=0.0,
                        help='LO offset in Hz (0, 6e6, or 11e6)')
    parser.add_argument('--tx-gain', type=float, default=0.75,
                        help='TX normalized gain [0.0, 1.0]')
    parser.add_argument('--rx-gain', type=float, default=0.75,
                        help='RX normalized gain [0.0, 1.0]')
    parser.add_argument('--encoding', type=int, default=0, choices=range(8),
                        help='MCS index: ' + ', '.join(f'{i}={n}' for i, n in enumerate(ENCODING_NAMES)))
    parser.add_argument('--chan-est', type=int, default=0, choices=range(4),
                        help='Channel estimator: ' + ', '.join(f'{i}={n}' for i, n in enumerate(CHAN_EST_NAMES)))
    parser.add_argument('--pdu-length', type=int, default=500,
                        help='TX payload size in bytes')
    parser.add_argument('--interval', type=int, default=300,
                        help='TX strobe period in ms')
    parser.add_argument('--tcp-port', type=int, default=None,
                        help='TCP server port for external PDU input on TX side')
    parser.add_argument('--pcap', type=str, default=None,
                        help='Append received frames to this pcap file (RX only)')
    parser.add_argument('--iq-capture', type=str, default=None,
                        help='Write raw RX IQ (complex64/fc32) to this file '
                             '(RX only) for spectrum/seam diagnosis')
    parser.add_argument('--tx-serial', type=str, default='30DBC3D',
                        help='USRP serial for TX')
    parser.add_argument('--rx-serial', type=str, default='30EDB63',
                        help='USRP serial for RX')
    parser.add_argument('--src-mac', type=parse_mac_addr, default='23:23:23:23:23:23',
                        help='TX source MAC address')
    parser.add_argument('--dst-mac', type=parse_mac_addr, default='42:42:42:42:42:42',
                        help='TX destination MAC address')
    parser.add_argument('--bss-mac', type=parse_mac_addr, default='ff:ff:ff:ff:ff:ff',
                        help='TX BSS MAC address')
    args = parser.parse_args()

    top_blocks = []

    if args.mode in ('tx', 'both'):
        print(f"[TX] serial={args.tx_serial}  freq={args.freq/1e6:.3f} MHz  "
              f"rate={args.samp_rate/1e6:.0f} Msps  gain={args.tx_gain}  "
              f"encoding={ENCODING_NAMES[args.encoding]}  "
              f"interval={args.interval} ms  pdu_length={args.pdu_length} B")
        top_blocks.append(WifiTx(args))

    if args.mode in ('rx', 'both'):
        print(f"[RX] serial={args.rx_serial}  freq={args.freq/1e6:.3f} MHz  "
              f"rate={args.samp_rate/1e6:.0f} Msps  gain={args.rx_gain}  "
              f"chan_est={CHAN_EST_NAMES[args.chan_est]}"
              + (f"  pcap={args.pcap}" if args.pcap else ""))
        top_blocks.append(WifiRx(args))

    def shutdown(sig=None, frame=None):
        print("\n[info] shutting down...")
        for tb in top_blocks:
            tb.stop()
        for tb in top_blocks:
            tb.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    for tb in top_blocks:
        tb.start()

    print("[info] running — press Ctrl+C to stop")

    for tb in top_blocks:
        tb.wait()


if __name__ == '__main__':
    main()
