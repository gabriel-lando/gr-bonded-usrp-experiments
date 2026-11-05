#!/usr/bin/env bash
# Run wifi_bonded_rx.py in replay mode (reconstruction + PHY, no radios, as
# fast as it goes) on a recording made with --mode record, and print the
# reconstructor profile plus a per-thread CPU snapshot. Run from the repo root.
#   bench/replay_probe.sh <rec-prefix> <samp-rate> <log> [extra wifi_bonded_rx.py args...]
prefix=$1; rate=$2; log=$3; shift 3
BONDED_PROFILE=1 ./wifi_bonded_rx.py --mode replay --rec-prefix "$prefix" \
  --samp-rate "$rate" --max-noutput-items 32768 "$@" > "$log" 2>&1 &
pid=$!
sleep 2.5
top -H -b -d 1.5 -n 2 -p $pid | awk '/^top -/{n++} n==2' | sed -n '7,20p'
wait $pid
grep bonded_profile "$log" | sed -n '2,$p' | awk '{for(i=1;i<=NF;i++) if($i~/^(out_msps|in_msps|avg_ms|calls)=/) printf "%s ", $i; print ""}'
