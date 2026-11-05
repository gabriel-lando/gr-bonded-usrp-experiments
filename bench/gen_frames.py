#!/usr/bin/env python3
#
# Generate a synthetic 802.11a IQ test vector for the PHY throughput benchmark.
#
# The 802.11a waveform in samples does not depend on the sample rate (the rate
# only labels the bandwidth), so one file serves every rate: processing N
# samples/s offline is exactly what the PHY needs to keep up with N MS/s live.
#
# Output: complex64 file = frames separated by gaps, plus AWGN. --duty sets the
# fraction of samples occupied by frames (live tests at 300 ms interval are
# nearly idle; a busy real channel is much denser).
#
#   ./gen_frames.py --out frames_dense.cfile --duty 0.5
#   ./gen_frames.py --out frames_idle.cfile  --duty 0.0

import argparse
import os
import sys
import time

import numpy as np
import pmt
from gnuradio import blocks, gr
import ieee802_11

sys.path.append(os.environ.get('GRC_HIER_PATH', os.path.expanduser('~/.grc_gnuradio')))
from wifi_phy_hier import wifi_phy_hier


class FrameGen(gr.top_block):
    def __init__(self, path, encoding):
        gr.top_block.__init__(self, "frame gen", catch_exceptions=True)
        self.phy = wifi_phy_hier(bandwidth=20e6, chan_est=ieee802_11.LS,
                                 encoding=ieee802_11.Encoding(encoding),
                                 frequency=5.3e9, sensitivity=0.56)
        self.mac = ieee802_11.mac([0x23] * 6, [0x42] * 6, [0xff] * 6)
        self.null_src = blocks.vector_source_c((0,), False, 1, [])
        self.mult = blocks.multiply_const_cc(0.6)
        self.sink = blocks.file_sink(gr.sizeof_gr_complex, path, False)
        self.msg_connect((self.mac, 'phy out'), (self.phy, 'mac_in'))
        self.connect((self.null_src, 0), (self.phy, 0))
        self.connect((self.phy, 0), (self.mult, 0), (self.sink, 0))


def render_frames(n, pdu_len, encoding, tmp):
    """Run the real gr-ieee802-11 TX chain; return (n, frame_len) array."""
    tb = FrameGen(tmp, encoding)
    tb.start()
    port = pmt.intern('app in')
    for i in range(n):
        payload = bytes((i + k) & 0xff for k in range(pdu_len))
        tb.mac.to_basic_block()._post(
            port, pmt.cons(pmt.PMT_NIL, pmt.init_u8vector(pdu_len, list(payload))))
    # The TX chain is message-driven; wait until the file stops growing.
    last, stable = -1, 0
    while stable < 5:
        time.sleep(0.2)
        size = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        stable = stable + 1 if size == last and size > 0 else 0
        last = size
    tb.stop()
    tb.wait()
    iq = np.fromfile(tmp, dtype=np.complex64)
    os.remove(tmp)
    if len(iq) % n:
        sys.exit(f'TX output {len(iq)} samples is not a multiple of {n} frames')
    return iq.reshape(n, -1)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--out', required=True)
    p.add_argument('--frames', type=int, default=200)
    p.add_argument('--pdu-length', type=int, default=500)
    p.add_argument('--encoding', type=int, default=0, choices=range(8),
                   help='0 = BPSK 1/2 (6 Mb/s, what beacons use)')
    p.add_argument('--duty', type=float, default=0.3,
                   help='fraction of samples inside frames (0 = noise only)')
    p.add_argument('--snr-db', type=float, default=25.0)
    p.add_argument('--cfo-hz', type=float, default=0.0,
                   help='carrier offset at 20 MS/s; exercises the CFO correction '
                        '(20 ppm at 5.3 GHz is ~106 kHz)')
    p.add_argument('--seconds', type=float, default=2.0,
                   help='file length in seconds at 20 MS/s (file is looped)')
    args = p.parse_args()

    total = int(args.seconds * 20e6)
    rng = np.random.default_rng(1)

    if args.duty > 0:
        frames = render_frames(args.frames, args.pdu_length, args.encoding,
                               args.out + '.tmp')
        flen = frames.shape[1]
        period = int(flen / args.duty)
        sig = np.zeros(total, dtype=np.complex64)
        n_placed = 0
        for start in range(period - flen, total - flen, period):
            sig[start:start + flen] = frames[n_placed % len(frames)]
            n_placed += 1
        p_sig = np.mean(np.abs(frames) ** 2)
    else:
        flen, n_placed, p_sig = 0, 0, 0.36
        sig = np.zeros(total, dtype=np.complex64)

    if args.cfo_hz:
        n = np.arange(total)
        sig *= np.exp(2j * np.pi * args.cfo_hz / 20e6 * n).astype(np.complex64)

    noise_p = p_sig / 10 ** (args.snr_db / 10)
    noise = (rng.standard_normal(total) + 1j * rng.standard_normal(total)) * np.sqrt(noise_p / 2)
    (sig + noise.astype(np.complex64)).astype(np.complex64).tofile(args.out)
    print(f'{args.out}: {total} samples, {n_placed} frames of {flen} samples, '
          f'duty={n_placed * flen / total:.3f}, snr={args.snr_db} dB, '
          f'cfo={args.cfo_hz:.0f} Hz')


if __name__ == '__main__':
    main()
