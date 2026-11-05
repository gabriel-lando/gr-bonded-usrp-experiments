#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0
#
# Offline throughput benchmark of the gr-ieee802-11 RX PHY used by
# wifi_bonded_rx.py — no radios. Feeds a looped IQ file (gen_frames.py) as fast
# as the flowgraph will take it and reports:
#   * MS/s sustained  (= the live sample rate this PHY could keep up with)
#   * frames decoded  (sanity: a faster but broken chain must not "win")
#   * CPU% per GNU Radio thread (one thread per block)
#
# Reading the result (AGENTS.md §12): one thread near 100% = that block is the
# compute bottleneck; nothing near 100% while MS/s is capped = handoff/buffer
# stall between blocks.
#
#   ./phy_bench.py frames_d30.cfile
#   ./phy_bench.py frames_d30.cfile --stage frontend --min-buf 65536

import argparse
import os
import threading
import time

import pmt
from gnuradio import blocks, fft, gr
from gnuradio.fft import window
import ieee802_11


def thread_cpu():
    """{tid: (name, cpu_seconds)} for every thread of this process."""
    tick = os.sysconf('SC_CLK_TCK')
    out = {}
    for tid in os.listdir('/proc/self/task'):
        try:
            with open(f'/proc/self/task/{tid}/stat') as f:
                stat = f.read()
        except OSError:
            continue
        name = stat[stat.index('(') + 1:stat.rindex(')')]
        fields = stat[stat.rindex(')') + 2:].split()
        out[tid] = (name, (int(fields[11]) + int(fields[12])) / tick)
    return out


class FrameCounter(gr.basic_block):
    def __init__(self):
        gr.basic_block.__init__(self, 'frame_counter', None, None)
        self.count = 0
        self.message_port_register_in(pmt.intern('in'))
        self.set_msg_handler(pmt.intern('in'), self._handle)

    def _handle(self, msg):
        self.count += 1


class PhyBench(gr.top_block):
    def __init__(self, args):
        gr.top_block.__init__(self, 'phy bench', catch_exceptions=True)
        self.src = blocks.file_source(gr.sizeof_gr_complex, args.file, True)
        self.head = blocks.head(gr.sizeof_gr_complex, int(args.samples))
        self.connect((self.src, 0), (self.head, 0))
        self.front = []
        self._build_phy(args)
        if args.min_buf:
            for b in [self.src, self.head] + self.front:
                b.set_min_output_buffer(args.min_buf)

    def _build_phy(self, args):
        # Mirrors wifi_bonded_rx.py WifiBondedRx._build_phy exactly.
        delay_sc, window_size, sync_length = 16, 48, 320

        if args.phy_frontend == 'fused':
            # One block: sync_short with the Schmidl-Cox chain built in.
            self.sync_short = ieee802_11.sync_short_fused(0.56, 2, False, False)
            self.front = [self.sync_short]
            self.connect((self.head, 0), (self.sync_short, 0))
        else:
            self._build_frontend_blocks(delay_sc, window_size)
        self._build_backend(args, sync_length)

    def _build_frontend_blocks(self, delay_sc, window_size):
        self.delay_sc   = blocks.delay(gr.sizeof_gr_complex, delay_sc)
        self.conj       = blocks.conjugate_cc()
        self.mult_sc    = blocks.multiply_vcc(1)
        self.ma_cc      = blocks.moving_average_cc(window_size, 1, 4000, 1)
        self.ma_ff      = blocks.moving_average_ff(window_size + 16, 1, 4000, 1)
        self.c2mag      = blocks.complex_to_mag(1)
        self.c2mag_sq   = blocks.complex_to_mag_squared(1)
        self.divide     = blocks.divide_ff(1)
        self.sync_short = ieee802_11.sync_short(0.56, 2, False, False)
        self.front = [self.delay_sc, self.conj, self.mult_sc, self.ma_cc,
                      self.ma_ff, self.c2mag, self.c2mag_sq, self.divide,
                      self.sync_short]

        self.connect((self.head, 0), (self.delay_sc, 0))
        self.connect((self.head, 0), (self.mult_sc, 0))
        self.connect((self.head, 0), (self.c2mag_sq, 0))
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

    def _build_backend(self, args, sync_length):
        self.delay_sync = blocks.delay(gr.sizeof_gr_complex, sync_length)
        self.counter = FrameCounter()
        if args.stage == 'frontend':
            self.null = blocks.null_sink(gr.sizeof_gr_complex)
            self.connect((self.sync_short, 0), (self.null, 0))
            return

        self.sync_long  = ieee802_11.sync_long(sync_length, False, False)
        self.s2v        = blocks.stream_to_vector(gr.sizeof_gr_complex, 64)
        self.ofdm_fft   = fft.fft_vcc(64, True, window.rectangular(64), True, 1)
        self.equalizer  = ieee802_11.frame_equalizer(
            ieee802_11.Equalizer(args.chan_est), 5.3e9, 20e6, False, False)
        self.decode_mac = ieee802_11.decode_mac(False, False)
        self.msg_connect((self.decode_mac, 'out'), (self.counter, 'in'))

        self.connect((self.sync_short, 0), (self.delay_sync, 0))
        self.connect((self.sync_short, 0), (self.sync_long, 0))
        self.connect((self.delay_sync, 0), (self.sync_long, 1))
        self.connect((self.sync_long, 0),  (self.s2v, 0))
        self.connect((self.s2v, 0),        (self.ofdm_fft, 0))
        self.connect((self.ofdm_fft, 0),   (self.equalizer, 0))
        self.connect((self.equalizer, 0),  (self.decode_mac, 0))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('file')
    p.add_argument('--samples', type=float, default=400e6,
                   help='samples to process (file is looped)')
    p.add_argument('--stage', choices=('full', 'frontend'), default='full',
                   help='frontend = stop after sync_short (Schmidl-Cox only)')
    p.add_argument('--max-noutput-items', type=int, default=32768,
                   help='same default as the live runs; 0 = GNU Radio default')
    p.add_argument('--min-buf', type=int, default=0,
                   help='set_min_output_buffer on source + front-end blocks')
    p.add_argument('--chan-est', type=int, default=0, choices=range(4))
    p.add_argument('--phy-frontend', choices=('blocks', 'fused'), default='blocks',
                   help='fused = ieee802_11.sync_short_fused (patched build only)')
    p.add_argument('--top', type=int, default=12, help='threads to list')
    p.add_argument('--window', type=float, default=4.0,
                   help='seconds of steady state to sample thread CPU over')
    args = p.parse_args()

    tb = PhyBench(args)
    t0 = time.monotonic()
    if args.max_noutput_items:
        tb.start(args.max_noutput_items)
    else:
        tb.start()
    waiter = threading.Thread(target=tb.wait)
    waiter.start()
    # Per-thread CPU is sampled over a steady-state window inside the run:
    # GNU Radio's block threads are gone once wait() returns.
    time.sleep(1.0)
    before, w0 = thread_cpu(), time.monotonic()
    waiter.join(args.window)
    after, w1 = thread_cpu(), time.monotonic()
    waiter.join()
    wall = time.monotonic() - t0
    window_s = w1 - w0

    msps = args.samples / wall / 1e6
    print(f'stage={args.stage} frontend={args.phy_frontend} max_noutput={args.max_noutput_items} '
          f'min_buf={args.min_buf} chan_est={args.chan_est}')
    print(f'RESULT samples={args.samples:.0f} wall={wall:.2f}s '
          f'rate={msps:.1f} MS/s frames={tb.counter.count} '
          f'frames_per_Msamp={tb.counter.count / (args.samples / 1e6):.2f}')
    # Share of the stream sync_short passed downstream as "inside a frame":
    # this, not the real frame duty, is what sync_long/equalizer pay for.
    print(f'sync_short COPY share={tb.sync_short.nitems_written(0) / args.samples:.3f}')
    rows = []
    for tid, (name, cpu) in after.items():
        cpu0 = before.get(tid, (name, 0.0))[1]
        if tid in before:
            rows.append((100 * (cpu - cpu0) / window_s, name, tid))
    rows.sort(reverse=True)
    total = sum(r[0] for r in rows)
    print(f'CPU total={total:.0f}% (of {os.cpu_count() * 100}%)  per thread:')
    for pct, name, tid in rows[:args.top]:
        print(f'  {pct:6.1f}%  {name}')


if __name__ == '__main__':
    main()
