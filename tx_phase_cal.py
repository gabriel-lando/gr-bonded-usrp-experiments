#!/usr/bin/env python3
#
# Closed-loop inter-radio phase correction for the bonded TX, using the
# receiver as the monitor (lab feedback path, like explicit-feedback
# beamforming).
#
# The two bonded TX radios share 10 MHz + PPS but have independent LOs, so the
# two half-bands arrive with an arbitrary relative phase that is fixed for the
# session (it drifts only a few degrees per minute).  Frames decode at any
# phase, but near 180 deg the seam subcarriers get ISI (the combined split
# filter h_A - h_B rings far past the cyclic prefix), which costs 64QAM.
#
# Loop (P, or PI with --ki): every --period s, run tx_frame_analyze.py --tail --summary over ssh on
# the RX host's growing IQ capture, read the median relative phase of the last
# frames, and send "B add <-gain*phase>" to wifi_bonded_tx.py --phase-ctl-port.
#
# Usage (TX started with --phase-ctl-port 5005, RX with --iq-capture FILE):
#   ./tx_phase_cal.py --capture /tmp/txb/run.fc32 [--rx-host rysen-s2c2-02]
# tx_frame_analyze.py must be on the RX host (default /tmp/txb/).

import argparse
import math
import socket
import subprocess
import time


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--capture', required=True, help='IQ capture path on the RX host')
    ap.add_argument('--rx-host', default='rysen-s2c2-02')
    ap.add_argument('--analyzer', default='/tmp/txb/tx_frame_analyze.py')
    ap.add_argument('--rate', type=float, default=10e6)
    ap.add_argument('--tx-port', type=int, default=5005)
    ap.add_argument('--period', type=float, default=5.0)
    ap.add_argument('--tail', type=float, default=1.5, help='seconds of capture per measurement')
    ap.add_argument('--gain', type=float, default=0.8, help='proportional loop gain (1 = full step)')
    ap.add_argument('--ki', type=float, default=0.0,
                    help='integral gain; > 0 tracks a steadily drifting phase with zero lag '
                         '(the bonded RX pair drifts ~9 deg/s after its alignment freezes)')
    ap.add_argument('--deadband', type=float, default=3.0, help='deg; no correction below this')
    ap.add_argument('--count', type=int, default=0, help='stop after N measurements (0 = forever)')
    ap.add_argument('--delay', type=float, default=0.0, help='seconds to wait before the first measurement')
    args = ap.parse_args()

    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    time.sleep(args.delay)
    n = 0
    integ = 0.0
    while args.count == 0 or n < args.count:
        n += 1
        cmd = (f"python3 {args.analyzer} {args.capture} --rate {args.rate} "
               f"--tail {args.tail} --summary 2>/dev/null")
        try:
            out = subprocess.run(['ssh', args.rx_host, cmd], capture_output=True,
                                 text=True, timeout=30).stdout.split()
        except subprocess.TimeoutExpired:
            out = []
        if len(out) == 5 and out[0] == 'REL' and out[1] != 'nan':
            rel = float(out[1])
            msg = ''
            integ += math.radians(rel)
            if abs(rel) > args.deadband or args.ki > 0:
                step = -args.gain * math.radians(rel) - args.ki * integ
                sk.sendto(f"B add {step:+.4f}".encode(), ('127.0.0.1', args.tx_port))
                msg = f" -> B {step:+.3f} rad"
            print(f"{time.time():.2f} [phase-cal] rel={rel:+6.1f} deg  frames={out[2]}  "
                  f"evm lo/hi={out[3]}/{out[4]} dB{msg}", flush=True)
        else:
            print(f"[phase-cal] no measurement ({' '.join(out) or 'ssh failed'})", flush=True)
        time.sleep(args.period)


if __name__ == '__main__':
    main()
