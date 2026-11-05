#!/usr/bin/env python3
#
# Offline throughput of overlap_reconstructor alone for a given target / overlap
# — no radios. Two looped noise sources stand in for the radios (with the same
# 262144-item output buffer bonded_source uses), so the block is fed exactly
# the way it is live. Uses the same frequency plan as wifi_bonded_rx.py.
#
#   ./recon_bench.py --samp-rate 80e6 --overlap 20e6 26.67e6 40e6

import argparse
import time

import numpy as np
from gnuradio import blocks, gr
from gnuradio import bonded_usrp


def plan(target, overlap):
    per_radio = 0.5 * target + 0.5 * overlap
    return per_radio, 0.5 * (per_radio - overlap)


class ReconBench(gr.top_block):
    def __init__(self, target, overlap, n_out):
        gr.top_block.__init__(self, 'recon bench', catch_exceptions=True)
        per_radio, off = plan(target, overlap)
        rng = np.random.default_rng(3)
        noise = ((rng.standard_normal(1 << 20) + 1j * rng.standard_normal(1 << 20))
                 * 0.01).astype(np.complex64).tolist()
        self.src_a = blocks.vector_source_c(noise, True)
        self.src_b = blocks.vector_source_c(noise[::-1], True)
        for s in (self.src_a, self.src_b):
            s.set_min_output_buffer(262144)
        self.recon = bonded_usrp.overlap_reconstructor(
            per_radio, target, target, overlap, -off, off)
        self.head = blocks.head(gr.sizeof_gr_complex, int(n_out))
        self.sink = blocks.null_sink(gr.sizeof_gr_complex)
        self.connect((self.src_a, 0), (self.recon, 0))
        self.connect((self.src_b, 0), (self.recon, 1))
        self.connect((self.recon, 0), (self.head, 0), (self.sink, 0))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--samp-rate', type=float, default=80e6, help='target T')
    p.add_argument('--overlap', type=float, nargs='+', required=True)
    p.add_argument('--seconds', type=float, default=10.0,
                   help='seconds of output at the target rate to process')
    args = p.parse_args()

    T = args.samp_rate
    for O in args.overlap:
        per_radio, _ = plan(T, O)
        tb = ReconBench(T, O, args.seconds * T)
        t0 = time.monotonic()
        tb.run(32768)
        wall = time.monotonic() - t0
        out = args.seconds * T / wall / 1e6
        print(f'T={T/1e6:.0f} O={O/1e6:.2f} ({100*O/T:.1f}%) per_radio={per_radio/1e6:.2f} '
              f'ratio={T/per_radio:.4f}  out={out:.1f} MS/s  in/radio={out*per_radio/T:.1f}  '
              f'realtime={out/(T/1e6):.2f}x', flush=True)


if __name__ == '__main__':
    main()
