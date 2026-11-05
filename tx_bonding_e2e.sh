#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0
#
# End-to-end TX bonding check: a plain single-radio RX (wifi_trx.py, no bonded
# code) runs on Machine 2 over ssh while this machine transmits, either with
# the bonded TX (two radios, hard split) or a single-radio baseline TX.
# Prints frames sent vs frames decoded.
#
# Usage:
#   ./tx_bonding_e2e.sh bonded|base [seconds] [-- extra wifi_bonded_tx.py / wifi_trx.py TX args]
# Env knobs (defaults in brackets):
#   RX_HOST [rysen-s2c2-02]  RX_SERIAL [30B56D6]  RX_GAIN [0.5]
#   FREQ [2.484e9]  RATE [10e6]  ENC [0]  INTERVAL [300]  TX_GAIN [0.5]
#   SERIAL_A [30DBC3C]  SERIAL_B [30EDB63]   (bonded TX radios; base uses SERIAL_A)
#   RX_BONDED=1  receive with the bonded RX (wifi_bonded_rx.py, radios RX_SERIAL +
#                RX_SERIAL_B [30DBC3D], gain RX_GAIN_DB [35]) instead of one radio
#   RX_ENV="VAR=val ..."  environment for the RX process (e.g. BONDED_PROFILE=1)
#   CAPTURE=1  also write the raw RX IQ to $RX_DIR/<tag>.fc32 on the RX host
#   TAG [<mode>_enc<ENC>]  RX_DIR [/tmp/txb]  LOG_DIR [./e2e_logs]
#   REMOTE_DIR [~/bonded-uhd/gr-bonded-usrp-experiments]  this repo on the RX host

set -u
MODE=${1:?bonded|base}; shift
DUR=${1:-30}; [ $# -gt 0 ] && shift
[ "${1:-}" = "--" ] && shift
EXTRA=("$@")

RX_HOST=${RX_HOST:-rysen-s2c2-02}
REMOTE_DIR=${REMOTE_DIR:-\~/bonded-uhd/gr-bonded-usrp-experiments}
RX_SERIAL=${RX_SERIAL:-30B56D6}
RX_GAIN=${RX_GAIN:-0.5}
FREQ=${FREQ:-2.484e9}
RATE=${RATE:-10e6}
ENC=${ENC:-0}
INTERVAL=${INTERVAL:-300}
TX_GAIN=${TX_GAIN:-0.5}
SERIAL_A=${SERIAL_A:-30DBC3C}
SERIAL_B=${SERIAL_B:-30EDB63}
TAG=${TAG:-${MODE}_enc${ENC}}
RX_DIR=${RX_DIR:-/tmp/txb}
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR=${LOG_DIR:-$HERE/e2e_logs}
mkdir -p "$LOG_DIR"

CAP=""
[ "${CAPTURE:-0}" = "1" ] && CAP="--iq-capture $RX_DIR/$TAG.fc32"

RX_SECS=$((DUR + 20))
if [ "${RX_BONDED:-0}" = "1" ]; then
    RX_CMD="./wifi_bonded_rx.py --mode live --freq $FREQ --samp-rate $RATE \
  --serial-a $RX_SERIAL --serial-b ${RX_SERIAL_B:-30DBC3D} --rx-gain-db ${RX_GAIN_DB:-35} $CAP"
else
    RX_CMD="./wifi_trx.py --mode rx --freq $FREQ --samp-rate $RATE \
  --rx-serial $RX_SERIAL --rx-gain $RX_GAIN $CAP"
fi
ssh "$RX_HOST" "mkdir -p $RX_DIR && cd $REMOTE_DIR && source .venv/bin/activate && \
  ${RX_ENV:-} timeout -s INT $RX_SECS stdbuf -oL -eL $RX_CMD 2>&1 | \
  python3 -u -c 'import sys,time; [print(\"%.2f\" % time.time(), l, end=\"\") for l in sys.stdin]' \
  > $RX_DIR/$TAG.rx.log" &
RX_PID=$!
sleep ${RX_INIT:-10}   # RX device init (bonded RX: ~15 s for PPS alignment)

cd "$HERE" && source .venv/bin/activate
if [ "$MODE" = "bonded" ]; then
    timeout -s INT "$DUR" ./wifi_bonded_tx.py --freq "$FREQ" --samp-rate "$RATE" \
        --serial-a "$SERIAL_A" --serial-b "$SERIAL_B" --tx-gain "$TX_GAIN" \
        --encoding "$ENC" --interval "$INTERVAL" "${EXTRA[@]}" > "$LOG_DIR/$TAG.tx.log" 2>&1
    SENT=$(grep "TX FINAL" "$LOG_DIR/$TAG.tx.log" | sed 's/.*total=\([0-9]*\).*/\1/')
    UND=$(grep -c underrun "$LOG_DIR/$TAG.tx.log")
else
    # wifi_trx.py has no per-frame log: estimate from run time after init.
    timeout -s INT "$DUR" ./wifi_trx.py --mode tx --freq "$FREQ" --samp-rate "$RATE" \
        --tx-serial "$SERIAL_A" --tx-gain "$TX_GAIN" --encoding "$ENC" \
        --interval "$INTERVAL" "${EXTRA[@]}" > "$LOG_DIR/$TAG.tx.log" 2>&1
    SENT="~$(( (DUR - 4) * 1000 / INTERVAL ))"
    UND=$(grep -o "U" "$LOG_DIR/$TAG.tx.log" | wc -l)
fi
wait $RX_PID
scp -q "$RX_HOST:$RX_DIR/$TAG.rx.log" "$LOG_DIR/"
DEC=$(grep -c "parse_mac :info: length: 524" "$LOG_DIR/$TAG.rx.log")
echo "[$TAG] sent=$SENT decoded=$DEC underruns=$UND"
