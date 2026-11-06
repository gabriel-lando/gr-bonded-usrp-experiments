# Offline benchmarks (no radios)

Three measurements, all run from this directory with the venv active
(`source ../.venv/bin/activate`):

| script | measures |
|---|---|
| `recon_bench.py` | `overlap_reconstructor` alone, fed like `bonded_source` feeds it live |
| `gen_frames.py` + `phy_bench.py` | the gr-ieee802-11 RX PHY used by `wifi_bonded_rx.py` |
| `replay_probe.sh` | reconstruction + PHY together on a recording (`wifi_bonded_rx.py --mode record`) |

Before any of them, set the CPU governor to `performance` and check that the
clock holds under load; a throttling CPU reads optimistic on short runs and
then decays:

```bash
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
watch -n1 "grep MHz /proc/cpuinfo | sort -u"     # in a second terminal while running
```

## Reconstructor

```bash
./recon_bench.py --samp-rate 80e6 --overlap 20e6          # 80 MHz channel, 25 % overlap
BONDED_THREADS=1 ./recon_bench.py --samp-rate 20e6 --overlap 5e6
```

Prints `out=… MS/s … realtime=…x`. Above 1× means the reconstructor alone
keeps up with that channel on this machine; the live pipeline (UHD receive
threads and the PHY on the same cores) needs margin on top. Reference: Ryzen 7
5700G, 80 MHz, 4 threads: ~2.0×.

## 802.11 PHY

```bash
./gen_frames.py --out frames_d30.cfile --duty 0.3            # 30% of samples in frames
./gen_frames.py --out frames_cfo.cfile --duty 0.3 --cfo-hz 100000 --snr-db 6
./phy_bench.py frames_d30.cfile                              # system gr-ieee802-11
./phy_bench.py frames_d30.cfile --phy-frontend fused         # needs the patched build
(source use_perf_build.sh; ./phy_bench.py frames_d30.cfile)  # patched build, see below
```

- `gen_frames.py` renders real frames with the gr-ieee802-11 TX chain
  (BPSK 1/2, 500 B, like beacons) and lays them out at a chosen duty cycle with
  AWGN and optional CFO. The 802.11a waveform in samples is rate-independent, so
  offline MS/s = the live sample rate the PHY could keep up with.
- `phy_bench.py` mirrors `WifiBondedRx._build_phy`, loops the file as fast as
  possible, counts decoded (CRC-valid) frames and samples CPU% per GNU Radio
  thread over a steady-state window. `--stage frontend` stops after
  `sync_short`; `--min-buf` / `--max-noutput-items` test buffering.
- Vectors are 320 MB each (2 s at 20 MS/s) and are not versioned; delete them
  when done.

## Reconstruction + PHY on a recording

```bash
cd ..   # repo root
timeout 8 ./wifi_bonded_rx.py --mode record --freq 2.484e9 --samp-rate 40e6 \
    --serial-a 30DBC3C --serial-b 30EDB63 --rx-gain-db 35 --rec-prefix /tmp/rec40
bench/replay_probe.sh /tmp/rec40 40e6 /tmp/replay40.log --freq 2.484e9
```

## gr-ieee802-11 patch

`gr-ieee802-11-perf.patch` holds the two gr-ieee802-11 performance commits
described below, as one `git am` series meant for a fork and an upstream PR:
the phasor frequency correction, and `sync_short_fused`, the fused
Schmidl-Cox front-end that decoding at 80 MHz needs. It applies to
gr-ieee802-11 `maint-3.10` (ad0598e). To build it into a separate prefix,
leaving the system install alone:

```bash
git clone -b maint-3.10 https://github.com/bastibl/gr-ieee802-11 ~/deps/gr-ieee802-11-perf
cd ~/deps/gr-ieee802-11-perf
git am <path-to>/gr-bonded-usrp-experiments/bench/gr-ieee802-11-perf.patch
mkdir build && cd build
cmake -DCMAKE_INSTALL_PREFIX=$HOME/deps/ieee-perf-install .. && make -j$(nproc) && make install
```

Then `source use_perf_build.sh` (`IEEE_PERF_PREFIX`, default
`~/deps/ieee-perf-install`) loads it in that shell. `wifi_bonded_rx.py` picks
the fused front-end on its own when the loaded build has it, and the e2e
scripts source `use_perf_build.sh` on the receiver by default (`RX_PERF=0`
turns that off). Both lab machines have it installed there.

## PHY results (2026-10-20, Ryzen 7 5700G, governor `performance`)

| test vector | system build | patched build | frames decoded |
|---|---|---|---|
| idle (noise only) | 94.6 MS/s | 92.9 MS/s | — |
| 3% duty | 77.6 | 90.4 | 820/820 both |
| 30% duty | **30.9** | **75.9** | 8240/8240 both |
| 70% duty | 29.1 | 67.1 | 19220/19220 both |
| 30%, CFO 100 kHz, SNR 6 dB | 28.9 | 75.7 | 1628 both (identical) |

### Root cause

Not buffering, not hardware: three blocks computed a complex exponential
(sin + cos) per sample to apply a frequency correction whose phase is linear
in the sample index.

1. **`sync_short`** (92% CPU, the original limit): `in * exp(j·f·n)` per
   sample, and after every detection it copies a fixed `MAX_SAMPLES = 540*80 =
   43200` samples regardless of the frame's length — at 30% duty ~90% of the
   stream goes through that path, which is why 30% and 70% duty cost the same.
2. **`sync_long`** (next limit after fixing 1): same per-sample `exp`.
3. **`frame_equalizer`** (limit on dense channels): per OFDM symbol, 64 `exp`
   for sampling-offset correction, 64 identical `exp(-beta)`, and
   `pmt::string_to_symbol("wifi_start")` (global-lock intern) on every symbol,
   including the ~360 skipped ones per detection.

Fix (first commit of `gr-ieee802-11-perf.patch`, branch `phy-perf` in the
worktree `~/deps/gr-ieee802-11-perf` on Machine 1): advance a phasor by one complex multiply per
sample, re-anchored to the exact `exp` every 512 samples; hoist the constant
`exp(-beta)` and the interned symbol. Decode counts are identical to the
original down to 6 dB SNR with 100 kHz CFO.

### What is left

- Buffer sizing makes no difference (75.7 ± 0.1 MS/s across
  `--min-buf 262144` and `--max-noutput-items` 0/32768/131072).
- The Schmidl-Cox front-end alone (~10 generic GNU Radio blocks) tops out at
  **~93 MS/s** with every thread at 40–60% — the cost of chaining many tiny
  blocks. That is what `sync_short_fused` (below, second commit) removes.
- On dense channels `frame_equalizer` is again ~99% (67 MS/s at 70% duty);
  next candidates there are the LS `equalize()` and the per-symbol tag lookup.
- Not yet measured live: the bonded reconstructor's own two ~99% threads share
  the same 8 cores, so live bonded+decode will land below these numbers.

## Fused Schmidl-Cox front-end (`--phy-frontend fused`)

`ieee802_11.sync_short_fused` replaces delay / conjugate / multiply / two
moving averages / complex_to_mag(_sq) / divide / sync_short with one block.
Same frames decoded on every vector (8180 = 8180 at 6 dB SNR, 100 kHz CFO).

| vector | blocks | fused |
|---|---|---|
| idle | 95 MS/s | 216 MS/s |
| 30% duty | 76 | 111 |
| 70% duty | 67 | 86 |
| CPU per MS/s (30% duty) | 10.6 %·core | 3.9 %·core |

Re-measured 2026-11-05 on 20 dB SNR vectors (system / phasor / phasor +
fused): 5 % duty 70.2 / 89.7 / 193.8 MS/s, 30 % duty 30.5 / 76.4 / 108.0 MS/s,
identical frame counts.

Live results of reconstruction and PHY together are in `../docs/LAB_NOTES.md`.
