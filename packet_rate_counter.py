#!/usr/bin/env python3
#
# Wraps a wifi_bonded_rx.py / wifi_trx.py RX process via stdin pipe, counts
# only packets matching an exact decode_mac -> parse_mac pair (filters out
# ambient/unrelated WiFi traffic in the area), and reports a running
# packets/minute rate timed from the FIRST matching packet.
#
# Usage (pipe an already-running RX process into this):
#   stdbuf -oL -eL ./wifi_bonded_rx.py --mode live ... 2>&1 | ./packet_rate_counter.py
#   stdbuf -oL -eL ./wifi_trx.py --mode rx ...        2>&1 | ./packet_rate_counter.py
#
# `stdbuf -oL -eL` forces line-buffering on the wrapped process so counts
# update live instead of arriving in bursts when the pipe's buffer fills.
#
# Defaults match the current test (encoding 0 / BPSK 1/2, --pdu-length 500):
#   decode_mac :info: encoding: 0 - length: 528 - symbols: 177
#   parse_mac  :info: length: 524
# Override --encoding/--length/--symbols/--parse-length if the TX side's
# --encoding or --pdu-length ever changes.

import sys
import re
import argparse
import time


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--encoding', type=int, default=0)
    ap.add_argument('--length', type=int, default=528)
    ap.add_argument('--symbols', type=int, default=177)
    ap.add_argument('--parse-length', type=int, default=524)
    args = ap.parse_args()

    decode_re = re.compile(
        r'decode_mac\s*:info:\s*encoding:\s*(\d+)\s*-\s*length:\s*(\d+)\s*-\s*symbols:\s*(\d+)'
    )
    parse_re = re.compile(r'parse_mac\s*:info:\s*length:\s*(\d+)')

    count = 0
    first_ts = None
    pending = False

    def report(final=False):
        if first_ts is None:
            if final:
                print("[pktcount] FINAL: no matching packets seen", flush=True)
            return
        elapsed = time.monotonic() - first_ts
        rate = (count / elapsed * 60.0) if elapsed > 0 else 0.0
        tag = "FINAL" if final else "pktcount"
        print(f"[{tag}] total={count}  elapsed={elapsed:7.1f}s  rate={rate:7.2f} pkt/min",
              flush=True)

    try:
        for line in sys.stdin:
            line = line.rstrip('\n')
            print(line, flush=True)  # pass through untouched

            m = decode_re.search(line)
            if m:
                enc, length, symbols = int(m.group(1)), int(m.group(2)), int(m.group(3))
                pending = (enc == args.encoding and length == args.length
                           and symbols == args.symbols)
                continue

            m = parse_re.search(line)
            if m and pending:
                pending = False
                if int(m.group(1)) != args.parse_length:
                    continue
                if first_ts is None:
                    first_ts = time.monotonic()
                    print(f"[pktcount] first matching packet at "
                          f"{time.strftime('%H:%M:%S')} — timer started", flush=True)
                count += 1
                report()
    except KeyboardInterrupt:
        pass
    finally:
        report(final=True)


if __name__ == '__main__':
    main()
