#!/usr/bin/env bash
#
# Reverse-direction end-to-end check: the bonded TX runs on Machine 2 over ssh
# and the bonded RX runs here on Machine 1. Prints frames sent vs frames
# decoded.
#
# Usage:
#   ./tx_bonding_e2e_reverse.sh [seconds]
# Env knobs (defaults in brackets):
#   TX_HOST [rysen-s2c2-02]  FREQ [2.484e9]  RATE [20e6]  ENC [0]
#   TX_GAIN [0.8]  TX_AMP [0.3]  SPLIT [ofdm]  RX_GAIN_DB [35]  INTERVAL [300]
#   TX_SERIAL_A [30B56D6]  TX_SERIAL_B [30DBC3D]   (bonded TX radios, remote)
#   RX_SERIAL_A [30DBC3C]  RX_SERIAL_B [30EDB63]   (bonded RX radios, here)
#   RX_ENV="VAR=val ..."  environment for the RX process
#   RX_ARGS="..."  extra arguments for wifi_bonded_rx.py (e.g. --phy-frontend fused)
#   RX_PERF [1]  RX loads the patched gr-ieee802-11 from bench/use_perf_build.sh's
#                prefix when it is there (0: the system one only)
#   TAG [rev_<rate>_enc<ENC>]  LOG_DIR [./e2e_logs]
#   REMOTE_DIR [~/bonded-uhd/gr-bonded-usrp-experiments]  this repo on the TX host

set -u
DUR=${1:-30}
TX_HOST=${TX_HOST:-rysen-s2c2-02}
REMOTE_DIR=${REMOTE_DIR:-\~/bonded-uhd/gr-bonded-usrp-experiments}
FREQ=${FREQ:-2.484e9}
RATE=${RATE:-20e6}
ENC=${ENC:-0}
TX_GAIN=${TX_GAIN:-0.8}
TX_AMP=${TX_AMP:-0.3}
INTERVAL=${INTERVAL:-300}
SPLIT=${SPLIT:-ofdm}
RX_GAIN_DB=${RX_GAIN_DB:-35}
TX_SERIAL_A=${TX_SERIAL_A:-30B56D6}
TX_SERIAL_B=${TX_SERIAL_B:-30DBC3D}
RX_SERIAL_A=${RX_SERIAL_A:-30DBC3C}
RX_SERIAL_B=${RX_SERIAL_B:-30EDB63}
TAG=${TAG:-rev_${RATE}_enc${ENC}}
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR=${LOG_DIR:-$HERE/e2e_logs}
mkdir -p "$LOG_DIR"

cd "$HERE" && source .venv/bin/activate
[ "${RX_PERF:-1}" = "1" ] && source bench/use_perf_build.sh
# RX first: it needs ~15 s for the PPS alignment.
( env ${RX_ENV:-} timeout -s INT $((DUR + 22)) stdbuf -oL -eL ./wifi_bonded_rx.py --mode live \
    --freq "$FREQ" --samp-rate "$RATE" --serial-a "$RX_SERIAL_A" --serial-b "$RX_SERIAL_B" \
    --rx-gain-db "$RX_GAIN_DB" ${RX_ARGS:-} > "$LOG_DIR/$TAG.rx.log" 2>&1 ) &
RX_PID=$!
sleep 20
ssh "$TX_HOST" "cd $REMOTE_DIR && source .venv/bin/activate && \
  timeout -s INT $DUR ./wifi_bonded_tx.py --freq $FREQ --samp-rate $RATE \
  --serial-a $TX_SERIAL_A --serial-b $TX_SERIAL_B --tx-gain $TX_GAIN --encoding $ENC \
  --split $SPLIT --tx-amplitude $TX_AMP --interval $INTERVAL" > "$LOG_DIR/$TAG.tx.log" 2>&1
wait $RX_PID
SENT=$(grep "TX FINAL" "$LOG_DIR/$TAG.tx.log" | sed 's/.*total=\([0-9]*\).*/\1/')
DEC=$(grep -c "parse_mac :info: length: 524" "$LOG_DIR/$TAG.rx.log")
echo "[$TAG] sent=$SENT decoded=$DEC underruns=$(grep -c underrun "$LOG_DIR/$TAG.tx.log")" \
     "rx_overflow_lines=$(grep -c overflow "$LOG_DIR/$TAG.rx.log")"
