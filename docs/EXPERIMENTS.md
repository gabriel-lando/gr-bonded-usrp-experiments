# Running the bonded TX and RX experiments

These scripts run over-the-air 802.11 experiments with the blocks from
[gr-bonded-usrp](https://github.com/gabriel-lando/gr-bonded-usrp). They use
two Ettus B210 radios as one wider transmitter, two as one wider receiver, or
both. What the blocks do and how to build them is documented in that repo. This
file only covers the experiments.

## 1. Setup (once per machine)

1. **gr-bonded-usrp.** Build and install it as its README describes, then
   check it:
   `python3 -c "from gnuradio import bonded_usrp"`.
2. **gr-ieee802-11 and gr-foo** (the 802.11 PHY/MAC used by every script),
   built from source for GNU Radio 3.10 and installed. To decode above
   60 MHz, also build gr-ieee802-11 with `bench/gr-ieee802-11-perf.patch`
   into `~/deps/ieee-perf-install` (`bench/README.md`); the bonded receiver
   then uses its fused front-end.
3. **`wifi_phy_hier`**, the hierarchical PHY block that `wifi_bonded_tx.py`
   and `wifi_trx.py` import. It is generated from gr-ieee802-11 into
   `~/.grc_gnuradio/`:
   ```bash
   grcc <path-to>/gr-ieee802-11/examples/wifi_phy_hier.grc -o ~/.grc_gnuradio
   ```
   (`GRC_HIER_PATH` overrides the directory.)
4. **Python environment** for the scripts:
   ```bash
   ./setup_venv.sh            # creates .venv with --system-site-packages
   source .venv/bin/activate
   ```
5. **Hardware.** Every bonded pair (TX or RX) needs both radios on the
   **same machine**, connected to one external 10 MHz reference and PPS. A
   single-radio role can run anywhere.

Lab radios:

| machine | serials | used as |
|---|---|---|
| Machine 1 (`rysen-s2c2-01`) | `30DBC3C` (A, lower), `30EDB63` (B, upper) | bonded TX or bonded RX |
| Machine 2 (`rysen-s2c2-02`) | `30B56D6` (A), `30DBC3D` (B) | bonded RX or TX, single-radio RX/TX |

The examples use channel 14 (`--freq 2.484e9`). **Transmitter and receiver
must use the same `--freq` and `--samp-rate`**: the sample rate *is* the
802.11 channel width here. The scripts have different default frequencies, so
always pass `--freq` explicitly.

## 2. What to look at

- **RX side:** pipe the receiver through `packet_rate_counter.py`. It counts
  only our frames (exact `decode_mac`/`parse_mac` pair, which filters out other
  Wi-Fi traffic) and prints a running packets/minute.
- **TX side:** `wifi_bonded_tx.py` prints `[txcount] total=N ... pkt/min`
  whenever the sent count changes, and `[TX FINAL]` on exit, in the same format.
  `--interval 300` (the default) means 200 frames/min.
- Compare the two totals. `[bonded_transmitter] Device N: underrun` on TX or
  `[bonded_receiver] Device N: ... overflow(s)` on RX mean the host is not
  keeping up (see section 8).

## 3. Bonded TX → one-radio RX

Receiver on Machine 2 (a plain single-radio receiver, no bonded code):

```bash
cd ~/bonded-uhd/gr-bonded-usrp-experiments && source .venv/bin/activate
stdbuf -oL -eL ./wifi_trx.py --mode rx --freq 2.484e9 --samp-rate 20e6 \
    --rx-serial 30B56D6 --rx-gain 0.6 2>&1 | ./packet_rate_counter.py
```

Bonded transmitter on Machine 1:

```bash
./wifi_bonded_tx.py --freq 2.484e9 --samp-rate 20e6 \
    --serial-a 30DBC3C --serial-b 30EDB63 --tx-gain 0.8 --encoding 7
```

`--samp-rate` is the whole channel. `--encoding` selects the 802.11 MCS:
0 = BPSK 1/2 … 7 = 64-QAM 3/4. The default split (`--split ofdm`,
`ofdm_splitter`) runs each radio at `5/8 · samp-rate` at
`fc ∓ 12/64 · samp-rate`, the same plan as the bonded RX, and works up to
64-QAM at any relative phase of the two radios. `--split filter` uses
`band_splitter` instead (`samp-rate/2` at `fc ∓ 14/64 · samp-rate`; 64-QAM
then needs the phase loop of section 7). `--per-radio-rate` and
`--radio-offset` override the plan.

Drive levels: keep `--tx-amplitude` at its default 0.3 and set the power with
`--tx-gain`: 0.8 at 20 MHz, 0.9 from 40 MHz up. A wider channel spreads the
same power over more subcarrier bandwidth, so 64-QAM at 80 MHz needs the
extra gain (0.8 there: 92 % instead of 98 %); at 20 MHz, 0.9 saturates the
receivers next to the transmitter. At 0.4 the rare OFDM peaks still clip the
DACs, at 0.6 64-QAM loses many frames.

## 4. One-radio TX → bonded RX

Single-radio transmitter (any machine):

```bash
./wifi_trx.py --mode tx --freq 2.484e9 --samp-rate 20e6 \
    --tx-serial 30DBC3D --tx-gain 0.75 --encoding 0
```

Bonded receiver (the two radios of one machine):

```bash
stdbuf -oL -eL ./wifi_bonded_rx.py --mode live --freq 2.484e9 --samp-rate 20e6 \
    --serial-a 30DBC3C --serial-b 30EDB63 --rx-gain-db 35 2>&1 | ./packet_rate_counter.py
```

`--rx-gain-db 35` suits the radios stacked as in the lab; at 40 the second
radio of Machine 2 (30DBC3D) saturates on the bonded TX at gain 0.8, which
shows up as 64-QAM losses on the upper half of the channel. Use 38 at 60 MHz
for 64-QAM (less power per subcarrier).

The overlap defaults to 25 % of the channel, so each radio runs at
`0.625 · samp-rate`. `--no-decode` runs only acquisition and reconstruction,
to measure throughput. `--iq-capture FILE` saves the reconstructed channel.

## 5. Bonded TX → bonded RX (2 + 2 radios)

Start the receiver first. It needs ~15 s for the PPS alignment. Then start the
transmitter.

Machine 2, bonded RX:

```bash
source bench/use_perf_build.sh     # patched gr-ieee802-11, needed above 60 MHz
stdbuf -oL -eL ./wifi_bonded_rx.py --mode live --freq 2.484e9 --samp-rate 80e6 \
    --serial-a 30B56D6 --serial-b 30DBC3D --rx-gain-db 35 2>&1 | ./packet_rate_counter.py
```

Machine 1, bonded TX:

```bash
./wifi_bonded_tx.py --freq 2.484e9 --samp-rate 80e6 \
    --serial-a 30DBC3C --serial-b 30EDB63 --tx-gain 0.9 --encoding 7
```

The receiver's first line says which PHY front-end it uses
(`phy_frontend=fused` with the patched build). At 80 MHz each radio runs at
50 MS/s; 0.1–0.5 overflows per second are normal there and cost a few
milliseconds each (the receiver steps over the gap).

The roles can be swapped (TX on Machine 2, RX on Machine 1) by swapping the
serials.

The receiver prints its alignment once the warmup is over, and again when
the tracking moves it (see the gr-bonded-usrp README):
`[overlap_reconstructor] alignment frozen: gain …, phase … deg, coherence …`.

### Automated run

`tx_bonding_e2e.sh` runs the receiver on Machine 2 over ssh and the
transmitter here, then prints `sent=… decoded=… underruns=…`:

```bash
./tx_bonding_e2e.sh bonded 30                                  # bonded TX -> 1-radio RX, 10 MHz
RATE=20e6 TX_GAIN=0.8 RX_GAIN=0.6 ENC=7 ./tx_bonding_e2e.sh bonded 30
RX_BONDED=1 RATE=80e6 TX_GAIN=0.9 RX_INIT=20 ENC=7 ./tx_bonding_e2e.sh bonded 30   # 2 + 2
./tx_bonding_e2e.sh base 30                                    # single-radio TX baseline
RATE=80e6 TX_GAIN=0.9 ENC=7 ./tx_bonding_e2e_reverse.sh 30     # 2 + 2, TX on Machine 2
```

Other variables (see the script headers): `ENC`, `INTERVAL`, `FREQ`,
`RX_SERIAL(_B)`, `RX_GAIN_DB` (default 35), `RX_ENV` (environment for the
receiver), `RX_ARGS` (extra receiver arguments), `RX_PERF=0` (receiver with
the system gr-ieee802-11 only), `CAPTURE=1` (save the receiver's IQ on
Machine 2), `TAG`. Extra `wifi_bonded_tx.py` arguments go after `--` (for
example `-- --split filter`). Logs go to `e2e_logs/`.

### Reference results, 2 + 2 up to 80 MHz (branch gl/80mhz, 2026-11-05)

64-QAM 3/4, `--tx-amplitude 0.3`, TX gain 0.8 at 20 MHz and 0.9 above,
bonded RX at 35 dB with the patched PHY; 30 s runs of ~78 frames unless
noted.

| direction | 20 MHz | 40 MHz | 60 MHz | 80 MHz |
|---|---|---|---|---|
| M1 TX → M2 RX | 79/79 | 79/79 | 75/78, 76/78, 77/77 (one session 58/78) | 77/77; 176/178 over 60 s; 373/379 over 2 min; at 20 frames/s 450–466 of 456–473 in five sessions (one 321/472) |
| M2 TX → M1 RX | 78/78 | 79/79 | 79/79, 77/78 | 79/79; 178/178 over 60 s; 378/379 over 2 min; at 20 frames/s 467/470, 454/455 |

At 80 MHz, BPSK: 78/79 (474/475 at 20 frames/s), 77/77 reversed; 16-QAM:
78/79, 80/80 reversed. With TX gain 0.8 at 80 MHz, 64-QAM drops to ~92 %.
The history of the 80 MHz work is in `LAB_NOTES.md` §18.

### Earlier reference results (branch gl/64qam, 2026-11-05)

TX gain 0.8, `--tx-amplitude 0.4`, `--split ofdm`; bonded RX at 35 dB,
one-radio RX at 0.6; stock PHY.

| setup | MCS | 20 MHz | 40 MHz | 60 MHz |
|---|---|---|---|---|
| bonded TX (M1) → 1-radio RX (M2) | 64-QAM 3/4 | 78/79 | 78/78 | |
| bonded TX (M1) → bonded RX (M2) | 64-QAM 3/4 | 78/78, six more sessions 77–79 of 78–79, 374/378 over 2 min | 79/79, six more sessions 76–78 of 77–79 | 56/77; 69/78 at TX 0.85 + RX 38 dB |
| bonded TX (M1) → bonded RX (M2) | BPSK | 76/77 | 78/78 | 77/80, 79/80 |
| bonded TX (M2) → bonded RX (M1) | 64-QAM 3/4 | 78/79 | 80/80 | |
| bonded TX (M2) → bonded RX (M1) | BPSK | | 76/77 | 78/78 |
| `--split filter`, BPSK | | 78/79 (1-radio RX) | 77/77 (bonded RX) | |

Before this branch, 64-QAM 2/3 through a bonded TX *and* a bonded RX at
20 MHz decoded 14/129. At 60 MHz 64-QAM is limited by the link budget (EVM
about −20 dB on these radios), not by the bonding.

## 6. Offline: record, then reconstruct and decode

`wifi_bonded_rx.py` can split the receive side in two: record both radios'
raw streams to disk, then reconstruct and decode them later, as fast as the
CPU allows and without radios. Useful to replay the same capture with other
settings, or to measure the receive chain offline (`bench/replay_probe.sh`).

```bash
# with a transmitter running (sections 3-5), record ~8 s
timeout 8 ./wifi_bonded_rx.py --mode record --freq 2.484e9 --samp-rate 20e6 \
    --serial-a 30DBC3C --serial-b 30EDB63 --rx-gain-db 35 --rec-prefix /tmp/cap
# -> /tmp/cap_a.fc32 and /tmp/cap_b.fc32 (radio A, radio B; 12.5 MS/s each here)

# later, anywhere with gr-bonded-usrp installed
./wifi_bonded_rx.py --mode replay --freq 2.484e9 --samp-rate 20e6 \
    --rec-prefix /tmp/cap --pcap bonded.pcap 2>&1 | ./packet_rate_counter.py
```

Replay must use the same `--samp-rate` (and `--overlap`, if one was given)
as the recording. Two radios at 12.5 MS/s write 200 MB/s, so keep recordings
short and on a fast disk.

## 7. Dense constellations and the phase loop

With `--split ofdm` (the default) nothing is needed: each radio sends a
complete OFDM signal of its own subcarriers, so the relative phase of the two
TX radios does not matter. The bonded RX tracks its own alignment.

With `--split filter`, the two TX radios start every session with an
arbitrary relative phase. Up to 16-QAM this does not matter. For 64-QAM the
phase should be near 0°. `tx_phase_cal.py` closes the loop through the
receiver:

```bash
# Machine 2: receiver with an IQ capture
./wifi_trx.py --mode rx --freq 2.484e9 --samp-rate 10e6 --rx-serial 30B56D6 \
    --rx-gain 0.6 --iq-capture /tmp/txb/run.fc32 | ./packet_rate_counter.py
# Machine 1: transmitter with the phase control port, then the loop
./wifi_bonded_tx.py --freq 2.484e9 --samp-rate 10e6 --serial-a 30DBC3C \
    --serial-b 30EDB63 --tx-gain 0.7 --encoding 6 --split filter --phase-ctl-port 5005 &
./tx_phase_cal.py --capture /tmp/txb/run.fc32 --rate 10e6
```

`tx_frame_analyze.py` (which the loop runs on Machine 2) prints the per-frame
relative phase, delay and EVM of a capture (`--qam 64` for 64-QAM frames,
`--min-snr` to keep other 802.11 traffic out, `--per-k` for EVM per
subcarrier, `--min-len 0.02e-3` at 60 MHz). It must also be copied to
`/tmp/txb/` there.

## 8. Troubleshooting

- **Nothing decodes:** check that both sides use the same `--freq` and
  `--samp-rate`, and that the bonded pair locked (`reference locked` and
  `PPS alignment done` in the log). Each radio needs a 10 MHz and a PPS cable.
- **Underruns on TX / overflows on RX at high rates:** set the CPU governor to
  `performance`. Raise `BONDED_THREADS` for the receiver (default 4). Use
  `BONDED_PROFILE=1` / `BONDED_RX_PROFILE=1` to see where it falls behind (the
  gr-bonded-usrp README explains the profile lines).
- **64-QAM losses but BPSK fine:** check clipping first (`--tx-amplitude`
  0.4, not 0.6), then RX saturation (lower `--rx-gain-db`; with `CAPTURE=1`
  and `tx_frame_analyze.py --per-k`, a saturated radio shows as a whole half
  of the channel with worse EVM), then SNR (EVM per subcarrier near −20 dB
  is too little for 64-QAM 3/4).
- **Bonded RX overflows continuously (`recv` below the per-radio rate even
  with `--no-decode`):** check that gr-bonded-usrp was built as Release
  (`grep CMAKE_BUILD_TYPE build/CMakeCache.txt`); an unoptimized build was
  the whole problem on Machine 2. A few overflows per second at 80 MHz are
  normal.
- **60–80 MHz bonded RX decodes little, `served` below the per-radio rate
  in `BONDED_RX_PROFILE=1`:** the stock gr-ieee802-11 PHY cannot keep up;
  load the patched build (`source bench/use_perf_build.sh`, see the
  receiver's `phy_frontend=` line).
- **A radio is not found (`No devices found for serial …`):** another
  process still holds it, typically a transmitter or receiver left over from
  an interrupted run (`pgrep -a python3` on both machines).
- **`pthread_setschedparam` warnings** mean UHD could not raise thread priority
  without privileges. They are harmless at these rates.
- History, measurements and the bugs found along the way: `LAB_NOTES.md`.
