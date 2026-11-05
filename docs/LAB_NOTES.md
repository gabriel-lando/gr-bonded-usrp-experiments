# Lab notes — bonded B210 TX/RX (history up to 2026-11-05)

> This is the engineering log that used to be `gr-bonded-usrp/AGENTS.md`,
> moved here on 2026-11-05 when that repo's docs were reduced to what the
> module itself contains. Section numbers below refer to this file. File
> paths such as `lib/...` and `tests/...` are in gr-bonded-usrp. Later changes
> are in gr-bonded-usrp's history, and how to run the experiments is in
> `EXPERIMENTS.md` here.
>
> 2026-11-05 code review in gr-bonded-usrp (squashed into its main branch):
> the frozen reconstruction path restarted its own timeline at the freeze,
> which changed each radio's translation phase differently and left the
> frozen alignment off: a static attenuation/notch around the RX seam whose
> depth depended on where the freeze fell (in the edge-aligned 20 MHz plan,
> about -1.6 dB in the offline test). Fixed (the fused filters now follow the
> warmup's input and share its rotation phase). RX overflows were also never
> counted or logged (the check ran after skipping empty recv() returns).
> `BONDED_TX_DUMP` and `BONDED_TX_FRAME_LOG` were removed; the reconstructor
> block lost its unused `fft_size` argument.

## 1. Goal

Bond two B210s into one wideband channel — **RX and TX use deliberately
different strategies**, not a shared overlap/split approach:

- **RX (thesis Part 1):** **intentional frequency overlap**, NOT a hard split.
  Each radio tunes to `fc ± T/4` and captures a wider slice sharing a
  configurable overlap centred on `fc`; the overlap aligns the radios
  (gain/phase) and crossfades them into one contiguous **20 MHz** wideband
  baseband, decoding real **802.11a/g** packets. Corner discard on the
  out-of-band edges. (See §13 — this exists because an early
  hard-split + naive combine produced 0 decoded packets.)
- **TX (Part 2):** **hard split at DC**, the opposite of RX. Each radio
  transmits its own half (seam on the unused DC-null OFDM subcarrier) and a
  standard 802.11 receiver's per-subcarrier LTF equalizer absorbs each radio's
  independent gain/phase/delay with **no phase calibration**. **Validated live
  2026-11-03** (10 MHz channel, 5 MS/s per radio, single-radio RX): BPSK–16QAM
  ~100% at any inter-radio phase; 64QAM needs the relative phase near 0, which
  a receiver-fed loop provides (96%). An August test had concluded hard split
  cannot work; that was a band_splitter bug, see §8 "TX bonding works
  (2026-11-03)" and §13.

## 2. Hard rule (do not violate)

**All signal-processing/DSP logic lives in C++** (the engine below). Python / GRC
is ONLY a thin flowgraph/test interface. See §13 for the two failures that drove this rule.

## 3. Architecture

```
Radio A (B210 @ fc-T/4) ┐ shared 10 MHz + PPS    Radio B (B210 @ fc+T/4) ┘
        │ bonded::bonded_receiver      — synchronized acquisition (lock 10 MHz,
        │                                detect PPS, latch common time=0; <1 µs)
        ▼ bonded::overlap_reconstructor — complex/IQ reconstruction DSP → fc baseband
        ▼ gr-ieee802-11 receiver        — Schmidl-Cox → FFT → equalize → Viterbi → MAC
TX path: bonded::bonded_transmitter (bonded_sink block) ← split wideband → 2 radios
```

GR blocks (this module): `bonded_source`, `overlap_reconstructor`, `bonded_sink`.
The C++ engine and the block wrappers are all under `lib/`.

## 4. Layout (everything in `lib/`)

| File                              | Role                                                                  |
| --------------------------------- | --------------------------------------------------------------------- |
| `overlap_reconstructor.{hpp,cpp}` | **THE** reconstruction DSP (time-domain VOLK)                         |
| `bonded_resample.hpp`             | shared polyphase resampler + VOLK dot helpers (header-only)           |
| `bonded_receiver.{hpp,cpp}`       | synchronized multi-device acquisition (RX)                            |
| `bonded_transmitter.{hpp,cpp}`    | synchronized multi-device transmit (TX)                               |
| `bonded_alignment.hpp`            | burst-alignment / ring-buffer helpers (header-only, `bonded::detail`) |
| `bonded_api.h`                    | `BONDED_API` export macro (replaced UHD's `UHD_API`)                  |
| `*_impl.{cc,h}`                   | GR block wrappers (thin: forecast + buffer plumbing only)             |
| `tests/*`                         | hardware-free Boost.Test ctests (see §6)                              |

**Namespace:** the engine is `namespace bonded` (was `uhd::usrp::bonded` when it
lived inside UHD). It depends on UHD only through the public API
(`uhd/usrp/multi_usrp.hpp`, `uhd/types/*`, `uhd/utils/thread.hpp`).

## 5. Build / install (fast — no libuhd rebuild)

Requires an **installed UHD** (official is fine) with dev headers + `UHDConfig.cmake`,
GNU Radio 3.10, Boost, and optionally VOLK / FFTW3f / OpenMP (auto-detected,
accelerate the reconstructor; built-in fallbacks otherwise).

```bash
cd gr-bonded-usrp && mkdir build && cd build
cmake .. && make -j16
ctest --output-on-failure                 # 3 hardware-free C++ tests
sudo make install && sudo ldconfig         # installs lib + python + grc
```

Editing the engine = rebuild **only this module** (seconds). No more
"install libuhd first", no root-owned UHD build dir. Rebuild + reinstall whenever
a C++ header changes (class layout) to avoid `free(): invalid pointer` from a
stale Python binding.

## 6. Validation

- `ctest` → `overlap_reconstructor_test` (alignment recovery, ±band-flatness, seam,
  streaming), `bonded_receiver_test` (burst alignment / ring extraction),
  `bonded_transmitter_test` (config + freq-plan geometry).
- `python3 ../samples/test_overlap_block.py` (or sibling repo) — GR flowgraph smoke
  test, no hardware.
- **Headline: DONE.** Live on real B210s, bonded RX decodes at parity with a
  standalone single-radio baseline (20 MHz: 95.8% then 98.7%; 40 MHz: 200.7 vs
  ~200 pkt/min). Bonding capability itself is validated to **80 MHz** real-time
  with `--no-decode`. See §12 for the measured ceilings and how to state the
  result.

## 7. DSP invariants — DO NOT regress

- **Complex (IQ) reconstruction** — phase preserved (OFDM). Power-spectrum
  stitching is unusable for decode.
- **Gain = power ratio** `sqrt(Σ|A|²/Σ|B|²)`, NOT `|Σ A·conj(B)|/Σ|B|²` (the latter
  collapses to 0 on bursty WiFi → "0 packets decoded"; this was a real bug).
- **Alignment is static** (shared 10 MHz) — estimate during a warmup, then **freeze**.
- **Configurable overlap:** smaller overlap lowers USB bandwidth but RAISES
  reconstruction CPU (sharper transition → longer FIRs). The CPU knob is `T`, not `O`.
- **Real-time: ACHIEVED** (this bullet previously said 0.24x live and
  offline-only — that was the Xeon VM era and is obsolete). On the Ryzen 7
  5700G, live real-time reconstruction runs with decode at 20 and 40 MHz, and
  without decode to 80 MHz. The fused complex-polyphase path, per-radio
  parallelism, the de-interleaved kernel and `bonded_source` buffer sizing all
  landed; see §8 and §12.

## 8. Live Performance / Parallelism Notes

> **READ §12 FIRST** for the current state, the runtime knobs and the
> diagnostic order. This section is the *chronological engineering log* of how
> the performance work got here, kept because several entries record things
> that must not be re-attempted. **Entries are in historical order and the
> early ones are obsolete** — the whole "20 MHz live overruns / real-time is
> hardware-limited / parallelism is a dead end" framing below was the Xeon VM
> and has been overturned on the Ryzen. Read to the end before acting on
> anything here.

### What the measurements said in the Xeon VM era (OBSOLETE — kept for context)

- **20 MHz live, full decode** still overruns.
- **20 MHz live, `--no-decode`** also overruns, so the first remaining live
  bottleneck is inside `bonded_source + overlap_reconstructor`, not only inside
  `gr-ieee802-11`.
- **20 MHz `--mode record`** mostly survives, so dual-radio acquisition alone is
  not the main remaining bottleneck.
- **5 MHz live** works and decodes packets, which localizes the failure to
  throughput scaling rather than a dead reconstruction algorithm.

### Profiling hook and how to use it

`lib/overlap_reconstructor.cpp` contains an opt-in profiler gated by the
environment variable `BONDED_PROFILE=1`. It prints one periodic summary line:

```text
[bonded_profile] recon calls=... parallel_calls=... avg_ms=... in_msps=... out_msps=... stage_pct={resample:..., rotate:..., align:..., crossfade:...}
```

- `avg_ms` = average wall-clock time per reconstructor `process()` call
- `in_msps` / `out_msps` = effective live throughput seen by the reconstructor
- `stage_pct` = approximate percentage of reconstructor wall time spent in:
  - `resample` — dual polyphase resamplers
  - `rotate` — complex frequency translation / phase rotator
  - `align` — warmup-only overlap-band estimation
  - `crossfade` — complementary FIR branches and final combine

Recommended profiling command from the sibling repo:

```bash
BONDED_PROFILE=1 ./wifi_bonded_rx.py \
  --mode live \
  --freq 2.484e9 \
  --samp-rate 20e6 \
  --serial-a 30B56D6 \
  --serial-b 30DBC3C \
  --rx-gain-db 30 \
  --max-noutput-items 1024 \
  --no-decode \
  --iq-capture /tmp/bonded_recon_20mhz.fc32
```

Useful sibling-script switches added during this investigation:

- `--no-decode` — runs `bonded_source -> overlap_reconstructor` only, skipping
  the 802.11 PHY so the reconstructor can be measured in isolation.
- `--iq-capture PATH` — writes the reconstructed fc32 stream to disk.

### Diagnosis: overhead-bound, not compute-bound

The arithmetic for 20 MHz is small (crossfade ≈ 2.6 Gmac/s, resample ≈ 0.5
Gmac/s) — a 16-core CPU has large headroom. The ~0.3× was caused by **overhead
and a large serial fraction**, not by lack of compute:

- the post-warmup path was capped at **2 threads** (`parallel sections
  num_threads(2)`), wasting 14 cores;
- the resampler `parallel for` was gated so it rarely fired at the small
  (~1k-sample) GNU Radio chunks, and per-call OpenMP region + heap/copy churn
  repeated ~20k times/sec;
- several **serial O(m) passes per call** (a per-output while-loop in the
  resampler, separate alpha-scale / add passes, an in-place rotate) limited
  parallel scaling regardless of core count.

### Stage 1–2 changes (current code — replaces the experimental notes)

All parallelism stays **inside the synchronous `process()` call** (OpenMP), which
preserves GNU Radio's block model — a threadpool inside `general_work()` would add
queueing/ownership/shutdown complexity around `_recon`, `_leftover`, FIR history,
and resampler state. What changed:

1. **Large work chunks** (`overlap_reconstructor_impl` ctor): `set_output_multiple`
   + `set_min_output_buffer` make the scheduler hand the block thousands of samples
   per `general_work` so every OpenMP region amortizes. Tunable via `BONDED_CHUNK`
   (default 8192).
2. **All-core crossfade** (`overlap_reconstructor.cpp`): the warmup/frozen branches
   are unified; both complementary FIRs **and** the final combine run as **one**
   `parallel for` over output samples. The constant alignment `alpha` is folded
   into the radio-2 history copy (FIR linearity), removing the serial alpha and add
   passes.
3. **Parallel rotate**: frequency translation is tiled with closed-form per-tile
   start phase (`phase·inc^i`) instead of a serial recurrence.
4. **Resampler** (`bonded_resample.hpp`): output count computed in **closed form**
   (was a serial per-output while-loop — a real scaling limiter); polyphase taps
   packed into one contiguous zero-padded buffer with a constant tap count.
5. **Inlined hot dots** (`dot_cr_hot`/`dot_cc_hot`): the per-sample FIR/resampler
   loops use inlined, auto-vectorized dots (compiled `-O3 -ffast-math -march=native
   -funroll-loops`) instead of VOLK's function-pointer dot, whose per-call dispatch
   dominates for short (K≈13–33) filters. VOLK is still used for one-shot dots.

### Measured result (offline scaling harness, 16-core Xeon E5-2650 v4 @ 2.2 GHz)

Profiler `in_msps`/`out_msps` (these time only `process()`):

- 1 thread: `in≈4.1`, `out≈5.5`; 16 threads: `in≈10`, `out≈13.4` (~5.2× per-call
  speedup; **~0.3× → ~0.67× real-time**).
- Steady-state split at 16 threads: `resample≈51%`, `crossfade≈40%`, `rotate≈8%` —
  balanced and all parallelized.
- **Thread budget** for full decode: do NOT take all 16 cores — leave headroom for
  the `gr-ieee802-11` PHY threads. Set `OMP_NUM_THREADS` (≈8–10) plus
  `OMP_PROC_BIND=close OMP_WAIT_POLICY=active`.

### LIVE validation (B210 rig) — the offline numbers do NOT transfer

Running the real flowgraph (`wifi_bonded_rx.py --no-decode`) overturned the offline
optimism. Steady-state `in_msps` (per-radio rate consumed; real-time needs ≥15):

| OMP threads (live) | `avg_ms` | `in_msps` | rotate share |
| ------------------ | -------- | --------- | ------------ |
| 1                  | ~1.8     | ~3.0      | 2%           |
| 4                  | ~1.3     | ~3.6      | 13%          |
| 10                 | ~1.4     | ~3.5      | 26%          |
| *offline, 16*      | *0.275*  | *~10*     | *8%*         |

**Fork-join OpenMP inside the block is the wrong model for the live pipeline.**
1→4 threads buys only ~20%, then plateaus; the rotate's share balloons 2%→13%→26%,
which is pure OMP thread-sync/wake overhead contending with UHD's high-priority USB
reader threads and the GR scheduler for cores. The offline 10-Msps result is
unreachable live. Net live behaviour ≈ **single-threaded**, and live 1-thread (~3.0)
is even slower than offline 1-thread (~4.0) because `bonded_source` shares the CPU.

Consequences for the RT strategy (supersedes the optimistic framing above):

- **The current algorithm cannot reach RT on this CPU even fully parallel** — the
  offline all-16-idle-core ceiling was only 0.67×, and live can never use all 16.
  Best live so far ≈ **0.24×**.
- What matters live is **single-thread work**, so the mandatory lever is reducing
  absolute work: **Stage 3 fused complex-polyphase per radio** (resample+translate+
  crossfade in one pass; removes the rotate and intermediate-buffer passes).
- Parallelism that *does* work live is **GR-native pipeline parallelism** — split the
  DSP into separate blocks (e.g. per-radio resample/translate → crossfade/combine) so
  the scheduler runs stages concurrently in their own threads (cheap, one thread per
  block) instead of per-call fork-join. Keep OMP off or `OMP_NUM_THREADS≈4` live.
- Set `OMP_WAIT_POLICY=passive` live (active spinning starves UHD); do **not** raise
  `--max-noutput-items` below `BONDED_CHUNK` or the block stalls.
- A monotonic in_msps decay within a run (3.5→1.5 over ~15 s) was observed — likely
  thermal throttling of the 2.2 GHz Xeon; confirm with `watch grep MHz /proc/cpuinfo`.
- Real-time 20 MHz on this specific CPU may be **hardware-limited**; the offline
  20 MHz decode remains the validated scientific claim.

### Stage 3 done — fused complex polyphase per radio (implemented)

The steady-state (frozen) path now uses ONE fused complex polyphase filter per radio
(`poly_resampler_c` in `bonded_resample.hpp`) whose prototype `g = conv(P,
upsample(h', decim))` embeds the resampler anti-alias, the per-radio translation
(modulated crossfade taps `h'[k]=h[k]e^{-jwk}`), and the crossfade band-shape; a cheap
output rotation + frozen-`alpha` + sum follow. The warmup path (separate resample +
rotate + overlap alignment + crossfade) is unchanged and runs until `warmup_samples`
(new config field) output samples, then freezes and switches to the fused path. A new
ctest (`*_frozen_steady_state`, with a low `warmup_samples`) covers the fused path —
previously the frozen branch had **zero** coverage.

Result: fused output is actually *flatter* than the warmup path (flatness
[0.997, 0.9996], spur 2.8e-5) and single-thread throughput improved ~**+15%**
(offline `in_msps` 4.1→4.73). The win is modest because the cost is genuinely FIR
arithmetic (~148 complex macs/output/radio), not reclaimable overhead — fusion mainly
removes the resampler's per-call bookkeeping and the separate rotate/crossfade passes.
Multi-thread offline is unchanged (~10 in_msps, already parallel/sync-bound).

### Parallelism is a dead end on this hardware (negative result — do not retry)

> **SUPERSEDED 2026-09-08 for the per-radio case, on Machine 1.** The section
> below is still an accurate record of the *Xeon* measurements, but its
> conclusion does not carry over to the Ryzen 7 5700G. Re-measured there: two
> independent reconstructor instances scale **1.94×** (12.87 + 12.29 vs 12.98
> alone), i.e. this CPU is *not* memory-bandwidth bound at this workload.
> Per-radio parallelism was re-implemented (see "Per-radio parallelism" below)
> and takes 40 MHz from 44% of real-time to exactly 100%. The OpenMP
> fork-join finding still holds — OMP threads change nothing on the fused
> path, because that path is a serial polyphase filter with no OMP regions.

Two parallelization strategies were measured and **both fail to speed up the
reconstructor** on the 16-core 2.2 GHz Xeon E5-2650 v4:

- **OpenMP fork-join** (inside the block): scales offline on an idle machine
  (~2.5× at 16 threads) but collapses live — per-call team sync contends with
  UHD's high-priority USB threads (1→4 threads = +20% then plateau; the cheap
  rotate/combine stages balloon to 15–25% = pure sync overhead).
- **Per-radio worker thread** (one persistent thread per radio's fused filter,
  OMP-free): offline it ran at ~138% CPU but delivered *less* throughput than a
  single core (3.9 vs 4.7 in_msps) — the two concurrent fused FIRs are
  **memory/cache-contention bound**, not core-starved. Reverted (kept the code
  single-threaded + clean).

Conclusion: the fused reconstructor is memory-bandwidth bound, so adding cores
does not help and live contention makes any in-block threading worse. **Best live
config: the fused engine with `OMP_NUM_THREADS=1`** (no team/sync overhead). Live
20 MHz real-time is hardware-limited on this CPU (compounded by a sustained-load
clock decay, ~3.5→1.5 in_msps over ~15 s — check the frequency governor / cooling).
The offline 20 MHz decode remains the validated scientific claim. If live RT is
required, the realistic levers are **different/faster hardware** or a **GPU**
channelizer — not more CPU threads.

### Update 2026-08-24 — new hardware (Machine 1, Ryzen 7 5700G) is much faster

All of §8 above was measured on the VM's 16-core 2.2 GHz Xeon E5-2650 v4.
Development has since moved to dedicated physical machines (see §10); Machine
1 is an **AMD Ryzen 7 5700G (8C/16T, up to 4.67 GHz)**. First offline
benchmark results there (`bonded-test-tools/benchmark/run_benchmark.sh`,
governor forced to `performance` — it defaulted to `powersave`, the exact
pitfall flagged above):

| threads | `in_msps` | vs Xeon single-thread (4.1) |
|---|---|---|
| 1 | **12.96** | ~3.2× |
| 2 | 13.34 | |
| 4 | 18.17 | already > 15 (RT threshold) |
| 8 | 21.80 | |
| 16 | 23.45 | |

Sustained single-thread run (600M samples, ~48s): `in_msps=12.41`, only a
~4% drop from the short-run number, clock observed steady at 3.7–4.5 GHz
(near the 4.67 GHz max) — **no thermal/governor decay** like the Xeon showed,
once the governor was set to `performance`.

**This changes the outlook materially: single-thread alone is already at 86%
of the RT threshold** (12.96 vs the ≥15 in_msps needed), and even the modest
2-4 thread offline scaling clears it.

### LIVE RE-VALIDATION on this hardware — real-time achieved (supersedes §8's Xeon verdict)

Ran the same live confirmation as the LIVE validation section above
(`OMP_NUM_THREADS=1 BONDED_PROFILE=1 ./wifi_bonded_rx.py --mode live --no-decode`),
on Machine 1 with its own two radios (`--serial-a 30DBC3C --serial-b 30EDB63`,
fc=2.412 GHz, 20 MHz combined, `--rx-gain-db 30 --max-noutput-items 32768`).

Result: **`in_msps` locked at 15.00** (the exact real-time threshold) for the
entire run (~1 minute, thousands of `process()` calls), essentially zero
variance (14.997–15.003), `avg_ms≈0.48`, `stage_pct{resample:~97.6%,
rotate:0, align:0, crossfade:~1.5%}` — matches the fused-path prediction
exactly. **No overflow (`O`) indicators, no decay.** Two harmless
`pthread_setschedparam` warnings (UHD couldn't get real-time thread priority
without elevated privileges) did not affect throughput.

**This overturns the Xeon-era "hardware-limited, live RT not achievable"
conclusion — on this hardware (Ryzen 7 5700G), live 20 MHz real-time
reconstruction IS achieved**, at least with `--no-decode` (decode-load not
yet tested). The offline-doesn't-transfer-to-live problem that plagued the
Xeon (UHD thread contention) does not reproduce here, most likely because
this CPU has enough single-thread headroom that `OMP_NUM_THREADS=1` alone
clears the bar without needing any parallelism to fight UHD for.

**Not yet tested:** decode enabled (adds `gr-ieee802-11` PHY threads
competing for the same cores — the actual pending validation from §6's
headline item), and the TX hard-split path live. Do those before declaring
the RT verdict final.

### DECODE + LIVE RT together — the §6 headline item is done

Re-ran the same live test **with decode enabled** (dropped `--no-decode`),
fed by a real over-the-air source: a single standalone B210 on Machine 2
running `wifi_trx.py --mode tx --freq 2.412e9 --samp-rate 20e6 --tx-serial
30DBC3D --tx-gain 0.75 --encoding 0 --interval 300` (matched to the bonded
RX's fc/bandwidth — `wifi_phy_hier`'s `bandwidth=` param sets the actual
OFDM subcarrier spacing, so TX and RX must agree on both `--freq` and
`--samp-rate`, not just `--freq`).

Result: **`decode_mac`/`parse_mac` fired continuously throughout the run**
(encoding 0 / BPSK 1/2, ~524–528 byte frames, multiple per profiler window)
— real packets, reconstructed from the two overlapping 15 MHz slices and
decoded by the unmodified `gr-ieee802-11` receiver — **while `in_msps`
stayed locked at ~15.00 and `out_msps` ~20.00**, matching the `--no-decode`
run almost exactly (`stage_pct{resample:~97.7%}`). Decode load did not
measurably cost reconstructor throughput.

**This resolves §6's headline pending item — live, real-time, decoded 20 MHz
bonded RX, validated end-to-end with actual hardware on both machines.**

### Decoded-frame-rate comparison vs single-radio baseline (§6 comparison, done)

Setup: Machine 2's `30DBC3D` transmitting (`wifi_trx.py --mode tx`,
`--encoding 0 --interval 300` → theoretical max 200 pkt/min), received
simultaneously by two independent listeners, each counted with
`bonded-test-tools/packet_rate_counter.py` (pipes RX stdout, counts only
exact `decode_mac`/`parse_mac` pairs matching the known frame — filters
ambient WiFi noise; timer starts at the first matched packet):

| Receiver | packets | elapsed | rate | % of theoretical max |
|---|---|---|---|---|
| **Bonded RX** (Machine 1, `30DBC3C`+`30EDB63`, live+decode, `in_msps` pinned at 15.0 throughout) | 526 | 164.7 s | **191.57 pkt/min** | 95.8% |
| **Standalone baseline** (Machine 2, `30B56D6`, co-located with the TX) | 546 | 163.9 s | **199.93 pkt/min** | ~100% |

The bonded pipeline decoded **95.8%** of what a standalone radio sitting
next to the TX decoded, while sustaining real-time reconstruction the whole
run. The ~4% gap is plausibly link-margin/SNR (the two receivers are on
physically separate machines/locations, not equalized distance to the TX) —
not yet isolated from a reconstruction-algorithm cost. If a cleaner
apples-to-apples number is ever needed, equalize RX-to-TX distance for both
sides in a repeat run.

**Remaining validation:** the TX hard-split path live (Machine 2's B210 was
used as a plain unbonded source for this test, not yet tested through
`bonded_transmitter`/`band_splitter` in its own bonded/split mode).

### TX hard-split live debugging (2026-08-24) — three real bugs fixed, one open negative result

> **SUPERSEDED 2026-11-03** — the "open negative result" below was a fourth
> bug (band_splitter held each frame's tail back until the next frame), not
> phase incoherence. TX now decodes up to 64QAM with no calibration. Read
> "TX bonding works (2026-11-03)" right after this entry; this one is kept for
> its three bug fixes, which still stand.

First-ever live test of the bonded TX path (`wifi_bonded_tx.py`, both Machine
1 radios split via `band_splitter` → `bonded_transmitter`, received by a
standard single-radio `wifi_trx.py --mode rx` on Machine 2). Uncovered and
fixed three genuine bugs; the underlying phase-coherence problem remains
**unresolved** — isolated (non-bursted) 802.11 frames still do not reliably
decode. Read this before touching TX-side code or re-attempting calibration.

**Bug 1 — `band_splitter` was never wired into the build.** It's designed to
live in `gnuradio.bonded_usrp` (its own header already says so — see
`bonded_sink.h`'s docstring), but its CMake target never existed. Fixed:
`lib/CMakeLists.txt` and `python/bonded_usrp/bindings/CMakeLists.txt` now
compile the sibling repo's `band_splitter.{cpp,hpp}` /
`band_splitter_impl.{cc,h}` / `band_splitter_python.cc` into this module
*when the sibling `bonded-test-tools/band_splitter/` dir is present*
(side-by-side checkout, matching §9); `include/gnuradio/bonded_usrp/band_splitter.h`
now ships as a real public header of this module. The sibling's own vendored
`bonded_resample.hpp` was **deleted** — it was a stale pre-fusion copy that
would have ODR-conflicted with this module's current (fused-engine)
`bonded_resample.hpp` once compiled into the same binary; band_splitter now
resolves it via this module's own `lib/` include path instead.

**Bug 2 — TX burst restart caused a ~300–600 µs amplitude ramp, corrupting
every isolated packet's 16 µs STF/LTF preamble.** The original
`bonded_transmitter::_send_loop_burst()` treated every queued 802.11 frame as
its own discrete UHD burst (`start_of_burst`/`end_of_burst` per frame,
nothing sent in the ~300 ms gaps between frames). Measured directly via
`--iq-capture` on the monitor: the B210 TX front-end needs ~300–600 µs to
ramp from idle to full power after a burst restart — far longer than the
16 µs STF+LTF preamble, so the equalizer trained on a still-rising channel
and corrupted the rest of the (already full-power) packet. Packets that
happened to land *inside* one long continuous burst (multiple frames sent
back-to-back, e.g. from Bug 3 below) were unaffected — only the first frame
of a burst hit the ramp — which is why an initial cluster of frames always
decoded while later isolated ones never did.

Fixed: `bonded_transmitter.cpp`'s send loop was rewritten
(`_send_loop_continuous()`) to keep the TX front-end **continuously
streaming for the whole session** — one `start_of_burst` at t0, one
`end_of_burst` at teardown, idle gaps filled with **equal-length zero
samples on every device every iteration** (this equal-length invariant, not
per-burst time_specs, is what now keeps the two half-bands sample-aligned).
Confirmed via a second `--iq-capture`: the ramp is gone, isolated bursts now
jump to full power almost instantly. Implementation notes for future
readers:
- The idle-fill loop must **never block/sleep** waiting on the queue — only
  `send()`'s own blocking-until-FIFO-has-room behaviour should pace it. An
  early version added a 2 ms `wait_for` poll on top of that, which starved
  the FIFO between calls and caused a continuous underrun flood; removed.
- The very first (timed) `send()` can return 0 if cold-start setup (device
  open, PPS wait) ate past the scheduled start instant — `send_all()` now
  detects this (`has_time_spec` still true after a 0-sample send) and
  retries **untimed** rather than hammering a stale timestamp forever.
- `freq_offset_hz`/`phase_offset_rad` correction (see Bug/Finding below) is
  now referenced to the **stream-wide cumulative sample count**, not a
  per-frame `k`, since frames are no longer independent bursts.
- Per-frame visibility (`[bonded_transmitter] frame: N samp/dev t=...`) was
  restored after being dropped in the rewrite — without it there's no way to
  tell periodic real frames from the zero-fill apart from reading `--pcap`
  sequence numbers. (Since 2026-11-03 that trace needs
  `BONDED_TX_FRAME_LOG=1`; the default is `bonded_sink.frames_sent()`, printed
  by `wifi_bonded_tx.py` as `[txcount]`/`[TX FINAL]` in the same format as the
  RX's `packet_rate_counter.py`.)

**Bug 3 — device configuration ran inside `start()`, racing the flowgraph's
own startup.** `bonded_sink_impl::start()` called the slow, blocking
`bonded_transmitter::configure()` (device open, register loopback tests, PPS
wait — several real seconds). GNU Radio does not guarantee block `start()`
ordering, and message-driven blocks upstream (`message_strobe`) run their
own timer thread as soon as *their* `start()` returns, regardless of when
`bonded_sink`'s finishes. Net effect: `message_strobe`→`mac`→`wifi_phy_hier`
generated and buffered several seconds' worth of real 802.11 frames
*before* `bonded_transmitter` ever drained its queue — confirmed directly in
the TX log (`frame: 151534 samp/dev` / `159736 samp/dev` on the very first
drain, vs a normal single-frame ~4096–8191 samples: `151534/8191≈18.5`,
matching the ~19 packets that always, and only, decoded). That backlog then
flushed as one giant continuous burst the instant streaming began — which is
what Bug 2's "packets inside one continuous burst decode fine" exception was
actually catching every time; it was never a signal that steady-state
worked.

Fixed: `bonded_sink_impl.cc` now calls `_tx->configure()` from the
**constructor** (synchronous, in Python, before `tb.start()` is ever called)
instead of from `start()`. Confirmed via the TX log: the first frame drained
is now a normal ~4096 samples, not a six-figure backlog.

**Open negative result — static phase/CFO correction is not enough.** With
Bugs 1–3 fixed, TX genuinely sends clean, evenly 300 ms-spaced isolated
frames for the entire session (confirmed continuously for 400+ s of `frame:`
log) — and RX **still does not decode them**, confirming the original design
assumption in §1 (equalizer alone suffices, no calibration needed) is
false: Schmidl-Cox sync/detection runs *before* equalization and is what's
failing, not the per-subcarrier correction downstream of it.

Measurement attempted: `bonded-test-tools/bonded_cw.py` (both TX radios emit
distinct-frequency CW tones) + a monitor B210 on Machine 2 +
`cw_analyze.py` recovers the inter-radio relative-phase trajectory. A
6.2 s capture, jump-segmented around ~910 ms-periodic USB-overflow
discontinuities (73 jumps, `resid<3°` filter → 48 clean segments), gave a
tight relative CFO estimate: **+11.9698 Hz, SEM 0.004 Hz** (up from an
earlier, cruder 1.5 s capture's `+11.94 Hz`). Applying this via
`bonded_transmitter`'s existing `freq_offset_hz`/`phase_offset_rad`
correction (`--freq-offset-b`, `wifi_bonded_tx.py`) gave **zero** decoded
packets over a 10-minute run — *worse* than the cruder `-11.94` value, which
had at least decoded a handful. A more precise CFO number making the result
worse rather than better is the key evidence that **CFO alone is not the
(whole) story** — most likely inter-radio phase noise/wander beyond a
constant-frequency correction, or a static phase-offset component that
resets unpredictably per session and was never separately calibrated
(`phase_offset_rad` was left at 0 throughout — see §13
for why measuring it needs a third, independent monitor radio and can't be
done with only Machine 1's two TX radios: the B210/AD9361 has **independent
TX and RX synthesizers**, so one radio's own RX cannot validly monitor its
own TX phase, let alone the other radio's).

**Where this leaves TX:** hard-split TX is validated at the DSP level
(`band_splitter`'s offline ctest passes: flatness, half-assignment, seam
attenuation, streaming-vs-one-shot all correct) and now streams cleanly at
the RF level (no ramp, low underrun, correct per-frame timing), but
**does not reliably produce decodable isolated packets live**, with or
without the measured static CFO correction. This is a genuine open problem,
not a remaining checklist item — the realistic next steps are (a) dynamic/
closed-loop phase tracking (the M3 approach the original design session
considered and set aside, believing the equalizer would make it
unnecessary — it doesn't), or (b) a TX architecture change reintroducing a
deliberate small overlap (mirroring the RX design) so phase alignment
becomes tractable the same way `overlap_reconstructor` already makes it
tractable on RX. Do not re-attempt a purely static/one-shot calibration
without first deciding which of these two directions to pursue — this
session already spent significant effort iterating on calibration precision
alone and it did not converge.

**The RX bonded path, by contrast, is fully validated** (§ "DECODE + LIVE RT
together" above) — treat RX and TX as being at very different maturity
levels going forward.

### TX bonding works (2026-11-03) — the "phase-coherence limit" was a pipeline bug

**Supersedes the negative result above.** Setup: bonded TX on Machine 1
(`30DBC3C` = A/lower, `30EDB63` = B/upper, 10 MHz channel, 5 MS/s per radio),
plain single-radio `wifi_trx.py --mode rx` at 10 MS/s on Machine 2
(`rysen-s2c2-02`, `30B56D6`). `bonded-test-tools/tx_bonding_e2e.sh` runs both
ends over ssh; `tx_frame_analyze.py` does per-frame LTF channel / EVM
analysis of an RX `--iq-capture`. Before the fixes: **3 of ~100 frames
decoded**. After (TX gain 0.7, RX gain 0.6, radios stacked, ~30 dB SNR):
BPSK 575/578 over 3 min, 16QAM 1/2 63/64, 64QAM 2/3 95–98% **when the
inter-radio phase is small** (see below; 22% at −114°, 10% in a 3 min run at
~180°), and 96% over 2 min with the receiver-fed phase loop. Also works at
20 MHz (10 MS/s per radio): BPSK 76/77, 16QAM 1/2 77/78. 0 underruns in every
run.

**Root cause: `band_splitter_impl` held each frame's tail until the next
frame.** `general_work()` consumed *all* available input but could only emit
what fit in `noutput_items` (which the scheduler had halved to satisfy
`forecast`), parking the rest in `_leftover`; `forecast()` then always
demanded ≥1 input sample, so with bursty 802.11 input the scheduler never
called the block again until the *next* frame arrived 300 ms later. On air
every frame was "tail of frame N-1 + gap + head of frame N" (the head
truncated), with the split point sliding ~360 samples/frame. Found by dumping
exactly what was handed to `send()` (`BONDED_TX_DUMP=<prefix>`, kept as a debug
hook). Fix: consume only `ceil(room*rate_in/rate_out)` input, and return 0
required input from `forecast()` while leftover is pending. (The old Python
smoke test's "GR terminates finite sources ~10% early" tolerance was this same
bug.) This was also why "the first ~19 frames always decoded": the startup
backlog was contiguous.

**The inter-radio phase does not break detection — but it does cost high
MCS.** With continuous streaming the relative phase φ of the halves is fixed
per session (random at each start, since each B210 retunes its own LO) and
drifts only a few degrees per minute (≈0.01 Hz, not the 12 Hz measured on the
old burst path). BPSK–16QAM decode at any φ: Schmidl-Cox holds because each
half is 16-sample periodic on its own (STF uses every 4th subcarrier), and the
LTF equalizer absorbs the per-half phase/delay. But the channel the receiver
sees is h_A + e^{jφ}·h_B: at φ=0 the complementary split filters sum to an
impulse, at large |φ| the sum rings for the split filter's length (±6 µs ≫
the 1.6 µs CP), so subcarriers within ~±6 of the seam get ISI (EVM −20 → −12
dB at ±1, while the single-radio baseline is flat). Measured in one session at
64QAM 2/3: φ = −114° → 22%, +50° → 98%, closed loop (~0°) → 96%.

**Phase loop (lab feedback through the receiver).** `bonded_sink` now has
`set_phase_offset(device, rad)` / `phase_offset(device)` (live; applied per
queued chunk in `bonded_transmitter`). `wifi_bonded_tx.py --phase-ctl-port
5005` accepts UDP "B <rad>" / "B add <rad>". `bonded-test-tools/tx_phase_cal.py`
runs `tx_frame_analyze.py --tail 1.5 --summary` over ssh on the RX's growing
`--iq-capture`, reads the median φ of our frames (bursts within 6 dB of the
strongest — other 802.11 traffic is on the channel too), and sends a
correction; it converges in 1–2 steps (4 s period) and holds ±4°. This is the
"monitor radio" the August notes wanted, except the monitor is the receiver
itself; a real system would need the receiver to report φ (e.g. from its
LTF estimate), or a calibration RX. The old static-calibration attempt failed
only because the captures it relied on were corrupted.

**What does matter — the seam subcarriers ±1/±2.** A subcarrier radiated by
both radios fades as |a + b·e^{jφ}| for the two split-filter gains a, b.
`tx_frame_analyze.py --per-k` matched a model of the old 25-tap split filter
exactly (|H| at ±1 ≈ 0.48 measured vs 0.46 predicted at φ≈150°), costing 64QAM
almost everything (7/60). Two changes:
- `band_splitter` `taps_per_phase` (default **64** → 129-tap filter for the
  2× split; the RX value 12 is unchanged) keeps the transition within ±1
  subcarrier. 64QAM 2/3: 7/60 → 58/61.
- Each radio's split cutoff is now **|radio offset|** (not the output
  Nyquist), so the radios can sit at **fc ∓ 14/64·T** instead of fc ∓ T/4.
  802.11 only uses ±26 of 32 subcarriers, so this still covers the signal while
  moving the seam from the radio's Nyquist edge (where the B210 DUC rolls off,
  |H|±1 ≈ 0.75) to 0.44·fs (|H|±1 ≈ 0.93). 64QAM 3/4: 59/78 → 70/77; mean EVM
  −21 → −23 dB. Oversampling the radios instead (6.25 MS/s at fc ∓ T/4) was
  measured worse (51/79). `wifi_bonded_tx.py` defaults to 14/64·T;
  `--radio-offset` / `--per-radio-rate` / `--split-taps` override.

**2 TX + 2 RX at 20 MHz (2026-11-03).** Code installed on Machine 2 too
(`/usr/local`; the old August copy is at `~/bonded-uhd/gr-bonded-usrp.bak-20261103`,
overwritten test-tools files in `~/bonded-uhd/bonded-test-tools.bak-20261103.tar`).
Bonded TX (Machine 1, 10 MS/s per radio at fc ∓ 4.375 MHz) → bonded RX
(`wifi_bonded_rx.py`, Machine 2, 30B56D6 + 30DBC3D, both locked to ref + PPS,
12.5 MS/s per radio), `RX_BONDED=1 RATE=20e6 ./tx_bonding_e2e.sh bonded 45`:
BPSK 126/127, QPSK 3/4 116/129, 16QAM 1/2 125/127, 64QAM 2/3 9–18% without
phase loop, **43% with it** (vs ~87% for the same bonded TX into one RX radio).
Finding: **the bonded RX's inter-radio phase drifts ~9°/s after its alignment
freezes** (the TX pair drifts ~0.3°/s). It shows up as a fast-rotating step at
the seam, which the TX loop can only track with the PI mode
(`tx_phase_cal.py --period 0.5 --tail 0.7 --ki 0.25`); the remaining 64QAM loss
is the RX's own crossfade region mixing two copies with a moving relative
phase. Fixing that means tracking the RX alignment instead of freezing it (§7
says freeze — that invariant was set with BPSK only), not more TX work.

**40 and 60 MHz (2026-11-03, later the same day).** Two more bugs, one per side:
- *TX:* one thread fed both radios in turn and could not keep up past
  ~2×20 MS/s (underruns at start-up at 40 MHz, continuously at 60). After an
  underrun the B210 resumes as soon as samples arrive, so that radio slipped
  against the other for good — visible in captures as the upper half's LTF
  missing from the lower half's frame (|H_hi| ≈ 0.05): 40 MHz decoded 1/79.
  Now a scheduler thread pushes the identical block sequence to one sender
  thread per radio, and any underrun re-opens both bursts on a common future
  time. 0 underruns at 20/40/60 MHz; bonded TX → one RX radio: 80/80 at 40,
  79/80 at 60. `band_splitter` itself only does ~33 MS/s in (0.55× real time
  at 60 MHz) — fine for bursty frames, a limit for continuous traffic.
- *RX:* `detail::extract_aligned_samples()` aligned only a block's first chunk
  by timestamp and concatenated the rest, so after an overflow the radio with
  the hole led the other by the missing samples for the rest of the session
  (lower half 3–20 samples late, decode 0% in some sessions, 100% in others —
  it also hit single-radio TX into the bonded RX). Now holes stay holes and the
  block is flagged, so `bonded_source` drops it on both radios. 2 TX + 2 RX at
  40 MHz: 5/5 sessions 78–79/79.
- *Still open at 60 MHz 2 TX + 2 RX:* 60–99% per session on Machine 2. Part is
  RX throughput (overflows; `BONDED_THREADS=4` helps, now the default), part looks like the two
  RX radios' relative phase wandering 30–200° *within* a frame (seen with a
  single-radio TX too, so RX side), which a receiver's single common-phase
  tracker cannot follow. The `drift` column of `tx_frame_analyze.py` measures
  it but is noisy; confirm before acting on it.

Smaller fix: `bonded_transmitter`'s send loop now lingers ≤2 ms for the rest
of a frame after real data before resuming zero-fill (one frame arrives as
several GR work() chunks; an immediate zero-fill between two would cut a hole
in it). Bounded and once per frame, so no underruns (0 in all runs).

Side observation: the single-radio `wifi_trx.py --mode tx` baseline decodes
**0** frames at 16QAM 1/2 on this rig (75 at BPSK) — the B210 burst-start
power ramp (~280 µs at −6 dB, visible in captures) wrecks the LTF amplitude
reference for QAM. The bonded TX streams continuously and does not have it,
so do not use that baseline as the reference above QPSK.

### Edge-aligned frequency plan (2026-09-08) — per-radio rate cut by 17%

The old plan captured `T/2 + O` per radio and put the radios at `fc ∓ T/4`,
which meant each radio captured `O/2` of spectrum *beyond* the channel edge
that was then thrown away (2.5 MHz/radio at T=20, O=5).

The plan is now **edge-aligned**: radio A covers exactly `[fc-T/2, fc+O/2]`
(channel's lower edge up to the overlap's upper edge), radio B its mirror, so
the union is exactly the target channel with **no captured-then-discarded
corner**:

- per-radio rate = `T/2 + O/2`, radios at `fc ∓ (T-O)/4`
- T=20, O=5 → 12.5 MS/s at fc∓3.75 (was 15 MS/s at fc∓5)
- T=40, O=10 → 25 MS/s at fc∓7.5 (was 30 MS/s at fc∓10)

Three wins at once: 17% less per-radio USB bandwidth, a resample ratio of 8/5
instead of 4/3 (33 vs 37 taps per polyphase phase → **~9% faster** per output
sample: 26.7 vs 24.5 MS/s output capability), and cleaner decimation (12.5
MS/s → MCR 50/decim 4; 25 MS/s → MCR 25/**decim 1**, no CIC at all — whereas
low per-radio rates such as 7 MS/s land on decim 9 and UHD warns about CIC
rolloff, which distorts exactly the band edges where the overlap lives).

The only thing given up is guard band: the channel edge now sits *at* the
radio's band edge instead of 2.5 MHz inside it. Measured at 20 MHz against the
standalone baseline: **153 vs 155 packets (98.7%)** — no measurable loss (for
802.11 the signal occupies 16.25 of the 20 MHz, leaving ~1.9 MHz of natural
margin). `--per-radio-rate T/2+O` reproduces the old plan for A/B tests.

### Per-radio parallelism (2026-09-08) — 40 MHz now runs at real-time

The two fused per-radio filters are independent and are ~98% of `process()`,
so radio 2 now runs on a persistent worker thread while the caller does radio
1. Handoff is a pair of atomic sequence counters with a bounded
spin→yield→sleep backoff, **not** a condition variable: each radio's share of
a call is only ~0.2 ms while a condvar wake costs ~200 µs, which is what sank
the first attempt. `BONDED_PARALLEL=0` restores the serial path.

Verified **bit-identical** output between serial and parallel (FNV-1a hash
over 524289 output samples of the fused path, 40/25 geometry: both
`b7c0ef608379580b`). All 3 ctests green.

Measured live (`--no-decode`, both radios on Machine 1):

| target | needs (in) | serial | parallel |
|---|---|---|---|
| 20 MHz | 12.5 MS/s | 12.50 (avg_ms 0.45, 75% duty) | **12.50** (avg_ms 0.23, 38% duty) |
| 40 MHz | 25 MS/s | 10.6–11.6 (~44%, ring drops ~⅔) | **25.00 / out 40.00** (avg_ms 0.24, 78% duty) |

`avg_ms` halves exactly — the handoff adds no measurable overhead.

### Ring-buffer accounting was O(chunks) per work() call — a feedback spiral

Found right after the above, when 40 MHz *with decode* still died: `in_msps`
started at ~24.4 (needs 25), then decayed monotonically over ~20 s to ~14.9
and settled, while `avg_ms` stayed flat at 0.25 and calls/sec fell. Flat
`avg_ms` + falling calls/sec = the reconstructor is starved, not slow.

Cause: `bonded_receiver::get_aligned_samples()` recomputed its "available"
counter with `detail::samples_available(ring)` — a full walk of the deque —
on **every** call, **while holding the ring mutex**. At 25 MS/s a 4 s ring is
~49k chunks (`continuous_buffer_seconds` × rate / `get_max_num_samps()`), so
the walk both burns a core and blocks the recv threads that need the same
mutex. That is a positive feedback loop: fall marginally behind → ring grows
→ walk costs more → fall further behind. It only shows up once something else
(here, the 802.11 PHY competing for CPU) pushes you slightly under real-time;
with `--no-decode` the same build locked at exactly 25.00.

Fixed: `extract_aligned_samples()` now reports how many samples it took out
(`extract_result::removed`, counting both consumed and alignment-discarded
samples) and the caller subtracts it — O(1). Verified equivalent to the old
rescan over 5114 randomized extractions (zero mismatches).

**Failure signature to recognize** (what 40 MHz looked like before this): the
reconstructor is *starved*, not slow — `avg_ms` stays normal while calls/sec
drops, `in_msps` decays over the first seconds and settles well under the
per-radio rate, CPU looks "extremely low", and **no overflow is ever
reported** because the ring buffer silently drops its oldest chunks. Decode
works for the first ~2 s (while the ring still has slack and hands out
contiguous samples) and then stops dead once the ring saturates and the
stream becomes discontinuous.

### De-interleaved (split re/im) polyphase kernel — 1.8x, 2026-09-08

After the parallelism and ring fixes, 40 MHz *with decode* still sat at
`in_msps` 23.9–24.2 against the 25 needed. Two hypotheses were tested and
**both were wrong**, which is worth recording so they are not retried:

- *"The GR output memcpy is the bottleneck."* `overlap_reconstructor` grew
  `process_into()` (frozen-path combine writes straight into the GR output
  buffer) plus `pending()`, wired into `general_work`. A/B via
  `BONDED_DIRECT_OUT=0`: **31.9 vs 31.3 MS/s — no measurable gain.** At 40 Msps
  that copy is ~320 MB/s against a ~10 GB/s memcpy, i.e. noise. The code was
  kept (it is not slower and removes a copy) but it fixed nothing.
- *"It is CPU contention / SMT pairing."* Under 6 competing busy threads the
  block still did 29.0 MS/s unpinned and 29.9 pinned to distinct physical
  cores — a 3% effect, nowhere near the observed deficit.

The actual cost was the hot dot product's **memory layout**. `dot_cc_hot()`
read `std::complex<float>` interleaved, so every iteration needed a shuffle to
separate re from im. Micro-benchmarked at K=33 on Zen 3:

| layout | rate | |
|---|---|---|
| interleaved | 57.9 Mdot/s | 15.3 GFLOP/s |
| **split re/im** | **110.3 Mdot/s** | **29.1 GFLOP/s** |

`poly_resampler_c` now stores taps as `poly_r`/`poly_i` and history as
`in_r`/`in_i` (both `vector<float>`), and `dot_cc_split()` runs four contiguous
FMA streams with no shuffles. The de-interleave in `feed()` is O(n) against
~53 MACs per input sample, so it is free. Output is **bit-identical**
serial vs parallel (`59633f668f641b15`) and `max|diff| = 0` vs the previous
build; all 3 ctests green.

Block throughput (25 -> 40 geometry, `recon_bench.py`, no radios):

| | before | after |
|---|---|---|
| serial (`BONDED_PARALLEL=0`) | ~16 MS/s | **32.2** (1.29x the 25 needed) |
| parallel | 29.6 (1.18x) | **57.9** (2.32x) |
| `avg_ms` | 0.172 | **0.092** |

The 20 MHz plan measures the same (32.1 / 58.4) — identical 8/5 ratio and tap
count, so the win applies to every bandwidth.

**Live result at 40 MHz with decode** (Machine 1, 30DBC3C + 30EDB63,
`--freq 2412e6 --samp-rate 40e6 --overlap 10e6 --rx-gain-db 40`):

| | `in_msps` | `avg_ms` | packets |
|---|---|---|---|
| parallel (default) | **25.0000 / out 40.0000, locked** | 0.156 | **200.7 pkt/min** |
| `BONDED_PARALLEL=0` | 23.5–24.0, still short | 0.288 | 190.9 pkt/min |

**Bonded RX at 40 MHz is now at parity with the ~200 pkt/min standalone
baseline**, and `in_msps` no longer decays. Keep parallelism on: the idea that
freeing the worker's spinning core would buy more for the 802.11 PHY was tested
and is **wrong** — serial loses on both throughput and packets.

Note `parallel_calls` in the profile line used to be incremented on
`use_parallel` (the size-eligibility flag) rather than on the actual dispatch,
so `BONDED_PARALLEL=0` still printed `parallel_calls == calls`. Fixed; the
warmup/FFT path no longer increments it at all.

**Lesson:** for this pipeline, profile *stage percentages* pointed at the right
place all along (resample was 96.6% of `process()`); the two failed hypotheses
were both about the ~3% that was *not* the resampler. Optimize the 96%.

### Overlap percentage sets the resampling ratio — and the DSP ceiling (2026-09-22)

The frequency plan is `per_radio = T/2 + O/2`, so **each radio always captures
at least half the target**, and `T/per_radio` — the resampler's rational ratio —
depends only on `O` as a *fraction* of `T`, never on absolute numbers. That
ratio dominates cost far more than the rate does:

| overlap | per_radio | ratio | block capacity (input MS/s) |
|---|---|---|---|
| **25% of T** (script default) | 0.625T | **8/5** | **57** |
| 20% of T | 0.60T | 5/3 | 48 |
| 12.5% of T (e.g. 80/45) | 0.5625T | 16/9 | 40 |
| 10% of T (e.g. 50/27.5) | 0.55T | 20/11 | 35 |

So **leave `--overlap` at the default**. Hand-picking a smaller absolute overlap
to save radio bandwidth backfires: it buys a few MS/s at the radio and costs far
more at the resampler.

**Hardware ceiling — CORRECTED 2026-09-22.** An earlier revision of this
section claimed the radios cap at ~29 MS/s each, giving `T <= 46 MHz`. **That
was wrong**: 29 was the *reconstructor's* limit, misattributed to the radios.
Direct measurement with `BONDED_RX_PROFILE=1` at 80 MHz shows `recv=45.00` on
both devices with **zero** overflows and `dropped=0.00` until the ring fills —
two B210s on one shared USB3 controller deliver 45 MS/s each without complaint,
and two independent `usrp_source` RX at 40 MS/s each decode fine side by side.
**The binding constraint is the reconstruction DSP, not the radios or USB.**

Measured in-pipeline capacity is about **0.86x** the standalone block bench
(the remainder is `general_work` overhead, which is why the recon thread's
process() duty tops out near 80% while the thread itself is pegged). With the
default 25% overlap the block benches at ~57 MS/s for any `T`, so real capacity
is ~49 MS/s of per-radio input:

| target | per_radio (25% ovl) | load vs ~49 | expected |
|---|---|---|---|
| 64 MHz | 40.00 | 81% | should lock |
| 70 MHz | 43.75 | 89% | marginal |
| 80 MHz | 50.00 | 102% | just short |

**`bonded_source` had no output buffer sizing — this was the real ceiling.**
`overlap_reconstructor_impl` calls `set_min_output_buffer()`; `bonded_source`
never did, so it ran on GNU Radio's default buffer. At 50 Msps that meant
~3.7k samples per `work()` call and ~10.9k scheduler handoffs per second.
Nothing was CPU-saturated (recon GR thread 84%, worker 100% but partly
spinning, `bonded_source` **30%**, ~312% total of 1600% available) and yet
throughput pinned at ~40 MS/s: a pure serialization-by-handoff stall.

Fixed by `set_min_output_buffer(262144)` in the constructor, overridable with
`BONDED_SRC_BUFFER`. Measured at 80 MHz (`--no-decode`, 5.3 GHz, default 25%
overlap, per-radio 50 MS/s):

| | `BONDED_SRC_BUFFER=8192` (old) | default 262144 |
|---|---|---|
| source calls/s | 10,900 | **380** |
| samples/call | ~3,700 | ~131,000 |
| `served` | 40.3 (ceiling) | **50.0** (what's needed) |
| `ring` | climbs to 4000 ms, then drops | 0-113 ms, flat |
| process RES | 2.5-3.1 GB (ring full) | 160-200 MB |

**80 MHz bonded RX runs in real time** — 4x the 20 MHz thesis baseline — with
`recv=50.00 served=49.5-50.9 dropped=0.00`. It is *marginal*: both recon
threads sit at ~99.5% CPU, process() duty is 90.4%, and occasional `OO` appear.
64 MHz has real headroom and is the safer number to quote.

**Lesson — check buffer sizing before optimizing anything.** Four hypotheses
were implemented and measured before this one, and all four were wrong: the
output memcpy (`BONDED_DIRECT_OUT` A/B: no gain), CPU/SMT contention (pinning:
3%), the copy under the ring mutex (`ringlock` only ~10%), and `bonded_source`'s
four-copies-per-sample path (thread was at 30%). The tell that pointed here was
**nothing at 100% CPU while throughput was pinned** — that is never a compute
problem. Any custom GR source running at tens of Msps needs an explicit
`set_min_output_buffer()`.

**Healthy vs failing, at a glance:** healthy is `wait` *high* (5-19%) with
`ring` near zero — the consumer is idling. Failing is `wait` low with `ring`
climbing steadily; drops begin only once `ring` hits
`continuous_buffer_seconds`, roughly 20 s later.

**Reading which limit bound:** compute duty cycle from the profile line as
`calls x avg_ms / 1000` (the window is exactly 1 s). ~100% = compute-bound;
anything well below while `in_msps` sits under the per-radio rate = starved by
the source. An 80 MHz attempt showed `calls=3650 avg_ms=0.217` -> 79% duty with
`in_msps` oscillating 25.7-27.4 and *not* decaying — supply-limited. Contrast
with the O(chunks) ring spiral, whose signature is a *monotonic* decay.

### The "~40 MS/s decode ceiling" was the reconstructor draining in series (2026-10-20)

With decode on, live output pinned at ~40 MS/s at any target, with **both** the
stock and a 2.4x-faster patched gr-ieee802-11. Offline the PHY alone ran the
real 80 MS/s air capture at ~94 MS/s, and `--mode replay` (recon + PHY) at ~76,
so neither stage was slow. `top -H` live: recon 87%, worker 69%, PHY blocks
~20% — nothing pegged, i.e. a handoff stall.

Cause: `overlap_reconstructor_impl::general_work` consumed **all** available
input (`consumed = navail`). Once `bonded_source` got its 262144-item buffer
(the 2026-09-22 fix), one call reconstructed ~420k outputs into `_leftover`
(5.5 ms, PHY idle with only the 64k output buffer to chew), then spent the next
calls only draining `_leftover` at the PHY's pace. Two stages in series:
1/(1/76 + 1/94) ≈ 42. Invisible with `--no-decode` (null_sink drains
instantly) and in replay (`file_source` hands out small chunks).

Fixed by consuming only `ceil(room * rate_in / rate_out)` input per call. Live
at 5.3 GHz with decode: 80 MHz 40 → **~75 MS/s** (now recon-bound, both threads
99.9%, ring still climbs — marginal as before); **64 MHz locked**
(`served=40.00`, `ring=0ms`, `wait=88%`, zero overflows over 45 s). ctests green.

Separately, gr-ieee802-11 itself had per-sample `exp()` in `sync_short`,
`sync_long` and `frame_equalizer`; that only matters when frames (or false
detections) are dense. Patch, benchmark and numbers:
`bonded-test-tools/phy_bench/README.md`.

### 80 MHz with decode in real time: sliced reconstructor + fused front-end (2026-10-20)

After the batching fix, 80 MHz with decode reached ~75 MS/s with both
reconstructor threads at 99.9%: the reconstructor (~1.25x real-time alone)
lost its margin to the ~10 PHY front-end threads sharing the 8 cores.

1. **Slices instead of one thread per radio.** `poly_resampler_c::feed()` is
   now `prepare()` / `compute(i0, i1)` / `commit()`; every output sample is
   independent, so `process()` cuts the output range into `BONDED_THREADS`
   slices, each doing both radios' FIR, their rotation (closed-form start
   phase) and the combine. Offline at 80 MHz: 1/2/3/4/6 threads = 50 / 92 /
   130 / 165 / 200 MS/s. Output vs the previous build: identical to -149 dB
   with 1 thread, -88 dB with 4 (rotator drift vs closed-form phase; harmless).
2. **`ieee802_11::sync_short_fused`** (patched gr-ieee802-11, see
   `bonded-test-tools/phy_bench/`): the nine-block Schmidl-Cox chain in one
   block, VOLK for the per-sample terms, `|a|^2 > t^2 p^2` instead of
   sqrt + divide. Identical decode counts; 2.75x less CPU per sample.
   `wifi_bonded_rx.py --phy-frontend fused`.

Live, 80 MHz at 5.3 GHz with decode, 40 s each:

| `BONDED_THREADS` | front-end | out_msps | ring | CPU |
|---|---|---|---|---|
| 2 | blocks | 70.4 | full, dropping | 642% |
| 4 | blocks | 79.0 | jumps to ~236 ms | 866% |
| 2 | fused | 79.0 | creeping, 0 -> 166 ms | 346% |
| **4** | **fused** | **80.0** | **0 ms throughout** | 507% |

Only the combination holds 80 MHz with margin.

### Important constraints on future performance work

- Keep all DSP in C++; do not move reconstruction logic into Python.
- Keep `ctest -R overlap_reconstructor_test --output-on-failure` green after any
  performance change.
- Do not replace complex reconstruction with power-spectrum stitching.
- Do not change the gain estimator away from the power-ratio form.
- Treat the current OpenMP work as **experimental** until a stable profile/test
  combination is captured and the live 20 MHz result improves reproducibly.
- If this still does not reach real time, the documented next major step remains:
  a **fused single complex-polyphase filter per radio**.

## 9. Sibling test repo (separate, side by side)

Test/capture/flowgraph scripts are **not** in this module (`band_splitter`
is, since 2026-11-03: `lib/band_splitter*`, `tests/band_splitter_test.cpp`). They live in a sibling repo
(was `host/samples/`): `wifi_bonded_rx.py`, `wifi_bonded_tx.py`, `wifi_trx.py`,
capture/analyze scripts, `OFFLINE_20MHZ_DECODE.md`, `bonded_cw.py`/
`cw_analyze.py` (CW-tone inter-radio phase measurement harness),
`tx_bonding_e2e.sh` (bonded/baseline TX here + plain RX on Machine 2 over
ssh, prints sent vs decoded), `tx_frame_analyze.py` (per-frame LTF channel,
inter-half phase/delay and per-subcarrier EVM of an RX IQ capture), and
`band_splitter/test_band_splitter.py` (GR smoke test of the installed block). **Captures (`*.fc32` ~248 MB,
`*.pcap`) are gitignored — never commit them.**

> **WARNING (2026-09-23): the sibling repo `bonded-test-tools` is NOT under
> version control and has no remote.** It holds every test script
> (`wifi_bonded_rx.py`, `wifi_trx.py`, `packet_rate_counter.py`,
> `install_gnuradio_build_deps.sh`), the docs `README.md`,
> `OFFLINE_20MHZ_DECODE.md`, `SEMINAR_BRIEFING.md`, `BENCHMARK.md`,
> `TX_BONDING_LIMITATION.md`, the `benchmark/` kit. `git init` it and push it
> somewhere. (This module's build no longer depends on it: `band_splitter`
> moved into `lib/` on 2026-11-03.)

## 10. Hardware

4× B210, split across **two independent physical machines** (was one VM — moved
for performance):

| Radio | Machine | Notes |
|---|---|---|
| `30DBC3C` | **Machine 1** (this repo, dev) | bonded RX pair, upper (B) |
| `30EDB63` | **Machine 1** (this repo, dev) | single-radio baseline |
| `30B56D6` | Machine 2 (test) | bonded RX pair, lower (A) |
| `30DBC3D` | Machine 2 (test) | TX |

A **bonded pair** (e.g. `30B56D6` + `30DBC3C`) MUST share an external 10 MHz ref
+ PPS — this only works when both radios of the pair are on the **same**
machine. Default `fc = 2.412 GHz`.

**Dev/test workflow (2 machines, no shared repo yet):** all development happens
on Machine 1; Machine 2 is only for running tests and doesn't need our code for
roles it plays as a "dumb" endpoint. Since a bonded pair must be co-located,
2-radio roles currently land on whichever machine holds `30B56D6`+`30DBC3C`...
but that pair is now split across machines, so **today's tests use single
radios in TX/RX roles, not the bonded pair**:

- **2 TX + 1 RX:** both transmitters on Machine 1 (`30DBC3C`, `30EDB63`);
  receiver on Machine 2 (doesn't need our code).
- **1 TX + 2 RX:** transmitter on Machine 2; both receivers on Machine 1
  (`30DBC3C`, `30EDB63`).
- **2 TX + 2 RX** (later, once the design is stable): requires Machine 2 to run
  this repo too — sync the repo to Machine 2 first.

Machine 2's copy of this repo (once synced) is expected to lag Machine 1 —
treat Machine 2 as test-only until told otherwise.

## 11. Dropped on extraction — do not resurrect

- Hard-split pipeline and the power-spectrum stitcher (display-only, discards phase).
- Legacy `bonded=true` device-args path (`bonded_usrp.*` + a `multi_usrp.cpp`
  patch) — it was the only edit to UHD core; the real RX/TX engine never used it.
- `spectrum_stitcher`, `frequency_plan` — unused by any block/engine.

Background for all three: §13.

## 12. Runtime knobs and how to diagnose a throughput problem

All are environment variables; none require a rebuild.

| Variable | Default | What it does |
|---|---|---|
| `BONDED_PROFILE=1` | off | Reconstructor profile line, once per second |
| `BONDED_RX_PROFILE=1` | off | Acquisition profile line (`bonded_receiver`), once per second |
| `BONDED_PARALLEL=0` | on | Force serial reconstruction (same as `BONDED_THREADS=1`) |
| `BONDED_THREADS=<n>` | 4 | Reconstructor threads; each takes a slice of every call's output range (both radios + combine). Default was 2 until 2026-11-05; 4 is what 80 MHz with decode needs |
| `BONDED_SRC_BUFFER=<items>` | 262144 | `bonded_source` output buffer. **Do not lower** — see §8 |
| `BONDED_CHUNK=<items>` | 8192 | Reconstructor `set_output_multiple`; its output buffer is `8x` this |
| `BONDED_DIRECT_OUT=0` | on | Restore the pre-`process_into` copy path (A/B only; no perf difference) |

The two profile lines:

```text
[bonded_profile]    recon calls=N parallel_calls=N avg_ms=X in_msps=X out_msps=X stage_pct={resample,rotate,align,crossfade}
[bonded_rx_profile] calls=N wait=X% ringlock=X% | dev0 recv=X served=X dropped=X ring=Xms | dev1 ...
```

### The diagnostic method (learned the hard way — follow this order)

Five hypotheses were implemented and measured across 2026-09-08/22 before the
real bottleneck was found. **Four were wrong.** The order below is what would
have found it immediately.

1. **Is the radio even delivering?** `recv=` in `[bonded_rx_profile]` is ground
   truth. If `recv` equals the per-radio rate, USB and UHD are fine — stop
   blaming them. (An 80 MHz run showed `recv=45.00` with zero overflows while
   `in_msps` sat at 26 — the radios were never the problem.)
2. **Is anything actually at 100% CPU?** `top -H -b -n 3 -d 2 -p $(pgrep -f wifi_bonded_rx.py)`.
   **Nothing at 100% while throughput is pinned is never a compute problem** —
   it is serialization by handoff, i.e. a buffer-sizing problem. This single
   check is what finally found it (recon thread 84%, `bonded_source` 30%,
   ~312% of 1600% available).
3. **Only then** look at compute. Use `stage_pct` and optimize the dominant
   stage; `resample` is ~96% of `process()`, so anything outside it is noise.

**Duty cycle** = `calls x avg_ms / 1000` (the profile window is exactly 1 s).
Near 100% = compute-bound. Well below, with `in_msps` under the per-radio rate
= starved.

**Failure signatures, distinguished:**

- `ring` climbing steadily with `dropped=0.00` → falling behind; drops begin
  ~20 s later when the ring hits `continuous_buffer_seconds` (4 s). **This is
  the early warning.** No overflow is ever printed — the ring drops silently.
- `ring` near zero with `wait` high (5-19%) → healthy, consumer idling.
- `in_msps` decaying *monotonically* with flat `avg_ms` → the O(chunks) ring
  rescan spiral (fixed 2026-09-08; see §8).

### Known ceilings (all measured, `--no-decode` unless noted)

| Limit | Value | Where |
|---|---|---|
| B210 delivery, 2 radios, one USB3 controller | **45-50 MS/s each**, no overflow | not a bottleneck |
| Reconstructor, 8/5 ratio (25% overlap) | ~57 MS/s bench, ~50 in pipeline | the real DSP limit |
| Bonded RX, real-time, no decode | **80 MHz** (marginal), **64 MHz** (comfortable) | §8 |
| gr-ieee802-11 PHY chain | ~94 MS/s idle channel; 31 → **76** MS/s at 30% frame duty with the phasor patch | `bonded-test-tools/phy_bench/` |
| Bonded RX, real-time, **with decode** | **80 MHz** with `--phy-frontend fused` and 4 reconstructor threads (the default since 2026-11-05) (ring 0); 64 MHz with stock settings | §8, 2026-10-20 |
| Standalone TX (`wifi_trx.py`) | ~40 MHz | external, user could not exceed it |

**State the thesis result in two parts** — they have different limits:

- **Bonding capability (RX):** validated to **80 MHz** real-time, 4x the 20 MHz
  baseline, `dropped=0.00`. This is the contribution.
- **End-to-end decode:** validated at **40 MHz**, 200.7 pkt/min, at parity with
  a standalone single-radio RX. (2026-10-20: the RX side now runs real-time with
  decode at 64 MHz — see §8; not yet validated with decoded packets above 40.)
  The 40 MHz cap was attributed to gr-ieee802-11; it was actually the
  reconstructor's own input batching. The single-radio TX (~40 MHz) remains an
  external limit on what can be *sent*.

## 13. Project history and prior decisions (consolidated 2026-09-23)

Previously held in Claude's per-machine memory, which does not survive a
reinstall. Folded in here so it travels with the repo.

### Genesis and why UHD is never patched

Work started 2026-06-02 *inside a UHD driver fork* (`~/uhd/host`), from a
pre-written `BONDING_IMPLEMENTATION_PLAN_FLEXIBLE.md` and a working
single-radio `wifi_trx.py`. Development there hit a wall: configuring
`multi_usrp` with custom bonding device-args was impractical from inside UHD's
internals. An architecture review confirmed the bonding code only ever called
UHD's **public** API, so on 2026-06-18 the whole engine was lifted out into
this self-contained OOT module with **zero UHD core patches**. The fork was
then deleted and official UHD re-cloned.

**Do not propose patching UHD core/internals for anything bonding-related** —
that path was deliberately tried and abandoned.

### Why all DSP is in C++ (§2's hard rule)

Two separate failures drove it, not style:

1. **Correctness.** The original prototype used two independent Python
   `usrp_source` blocks with a naive rotate+add. A USB overflow on *either*
   device desynced the combiner — no timestamp realignment existed, so a
   dropped chunk on one radio silently corrupted the other radio's stream.
   `bonded_receiver` fixes this by aligning on timestamps in ring buffers: a
   dropped chunk zero-fills only its own device.
2. **Performance.** Real-time needs SIMD-tight inner loops; Python/GRC blocks
   would be categorically slower.

**Never** suggest moving reconstruction/acquisition math to Python "for a quick
prototype" — that exact combination is the historical bug that produced
undecodable captures.

### Why RX uses overlap + complex crossfade (§1, §7)

The original decode pipeline did the **opposite** of the thesis design: a hard
split at DC with naive `rotate + add_cc` and **no** inter-radio alignment. A
small fixed LO phase offset destroyed the OFDM constellation → **0 packets
decoded**. A `spectrum_stitcher` block did crossfade but only on **power
spectra**, discarding phase — display-only, useless for decode.

The fix that *is* the RX thesis contribution: true complex (IQ) reconstruction —
estimate inter-radio gain/phase over the shared overlap (gain as a **power
ratio**; a raw cross-correlation collapses to 0 on bursty traffic, itself a
distinct historical bug), freeze it after warmup (valid because the shared
10 MHz reference makes the offset static), then combine with a phase-continuous
cos²/sin² crossfade.

**Never** revert to hard-split-plus-naive-add, and **never** stitch on
magnitude spectra.

### TX: the design assumption that was "disproven" — and then confirmed

> **2026-11-03: the hard-split design works as originally intended.** The
> August "disproof" below came from captures in which `band_splitter` was
> splitting every frame across two 300 ms-apart transmissions (§8 "TX bonding
> works (2026-11-03)"). With that fixed, frames up to 16QAM decode at any
> inter-radio phase with no calibration; 64QAM wants the phase near 0, done
> with a live receiver-fed loop (the phase is stable within a session, so this
> converges trivially). The text below is history.

The B210s share a 10 MHz OCXO so they are frequency-locked (~5 ppb) but each
has its **own AD9361 synthesizer** — they are **not phase-coherent**. Relative
LO phase is random at each retune and drifts (~12.8 Hz inter-frame measured).

Design evolution: a closed-loop calibration using a spare B210 as monitor was
designed (2026-06-16 afternoon), then discarded the same night in favour of a
**hard split at DC** on the theory that any standard 802.11 receiver's
per-subcarrier equalizer would absorb each radio's offset for free.

**That theory was tested live on 2026-08-24 and is false.** Schmidl-Cox
sync/detection runs *before* equalization, and detection is what fails under
inter-radio phase incoherence. Three real bugs were found and fixed along the
way (build wiring, a 300-600 µs TX amplitude ramp on burst restart, a startup
race queueing a false backlog burst) and isolated frames still do not reliably
decode. Careful CFO calibration made it **worse**: +11.94 Hz decoded a handful,
the more precise +11.9698 Hz (SEM 0.004 Hz) decoded **zero** over 10 minutes —
strong evidence that a constant-frequency correction is not the whole story.

**Do not re-attempt static/one-shot phase or CFO calibration** — that was
already iterated and did not converge. Two realistic directions remain:
(a) genuine closed-loop phase tracking with a real third monitor radio, or
(b) redesigning TX around a deliberate overlap, mirroring RX. A device's own RX
chain **cannot** monitor its own TX: the AD9361 has independent TX and RX
synthesizers, so their LO phases have no fixed relationship.

### Performance history (why old conclusions are stale)

The June 2026 checkpoint concluded on a 16-core 2.2 GHz Broadwell Xeon VM that
live 20 MHz RT was **hardware-limited** and that **per-radio parallelism was a
dead end** (a worker thread ran at 138% CPU below single-core throughput —
memory-bandwidth bound). Both conclusions were Xeon-specific and have been
**overturned** on the Ryzen 7 5700G: two reconstructor instances scale 1.94x,
and per-radio parallelism now works (see §8). OpenMP fork-join remains a dead
end — do not re-attempt it. Do check the CPU governor: it defaults to
`powersave` and must be `performance`.

### Original VM (if more history is ever needed)

`ssh gabriel@ubuntu-uhd` (username `gabriel`, not `glando`). Repo lived at
`~/bonded-usrp/{gr-bonded-usrp,bonded-test-tools}`. Claude session history:
`~/.claude/projects/-home-gabriel-bonded-usrp/` (post-extraction) and
`~/.claude/projects/-home-gabriel-uhd-host/` (the earlier UHD-fork era,
6 sessions 2026-06-02 to 2026-06-18). **This VM is not being reformatted.**

## 14. Machine environment

**Machine 1** = `rysen-s2c2-01`, user `glando`, Ubuntu 24.04.4, **AMD Ryzen 7
5700G** (8C/16T, up to 4.67 GHz). Radios `30DBC3C` + `30EDB63`.

- **CPU governor defaults to `powersave`** — force `performance` before any
  benchmark. Worth roughly 2x.
- Both B210s enumerate on **Bus 004** (a 2-port xHCI controller). Buses 002 and
  006 are also 10 Gbps and empty. The shared controller was measured **not** to
  be a bottleneck (2x 40 MS/s decoding simultaneously, no overflow), but
  splitting them across controllers is free headroom if ever needed.
- GNU Radio 3.10 built from source. Full apt dependency list:
  `bonded-test-tools/install_gnuradio_build_deps.sh` — run it instead of
  rediscovering deps one cmake error at a time. **Known gotcha:**
  `gnuradio-companion` stays disabled even with `python3-gi` present unless the
  GObject-Introspection typelibs are installed too: `gir1.2-gtk-3.0`,
  `python3-gi-cairo`, `gir1.2-pango-1.0` (a cmake log with `PYGI_FOUND=TRUE`
  but `GTK_GI_FOUND=FALSE` means exactly these three).
- Python venv for the test scripts: `bonded-test-tools/.venv`
  (`source .venv/bin/activate`). The GR module is imported as
  `from gnuradio import bonded_usrp`, **not** `import bonded_usrp`.

**Machine 2** = test-only, radios `30B56D6` + `30DBC3D`, does not run this repo.

## 15. 2026-11-05: code review, squash into gr-bonded-usrp main

Two more bugs found while adding tests:

- **Frozen RX path restarted its own timeline at the freeze** (see the note at
  the top). Probably why 2 TX + 2 RX at 60 MHz varied 60-99% per session: the
  seam attenuation depended on where the freeze fell. After the fix, 4/4
  sessions at 60 MHz decoded 77/77, 77/77, 80/80 and 78/79. That is a small
  sample, so it is consistent with the fix but does not prove it.
- **`bonded_sink` dropped what was still queued on stop.** GNU Radio stops
  the blocks as soon as a finite source is exhausted, so a file source lost
  its end (and the cut burst caused an underrun). Now the scheduler hands
  the rest of the queue to the senders, followed by an end marker, and
  `run()` returns once everything is on the air (1 s file: run() = 1.5 s,
  i.e. the 0.5 s start lead + 1.0 s of data, no underrun).

Other changes: RX overflows are now counted and logged (the check ran after
skipping empty recv() returns, which is where UHD reports them), the
transmitter's async-message threads now live from configure() until
destruction (destroying a configured transmitter that never streamed used
to call std::terminate), `bonded_sink` rejects malformed per-radio CSV
values instead of silently dropping them, and the GRC blocks now install to
share/gnuradio/grc/blocks (they were installed where GRC does not look).
The old copies in /usr/local/share/gr-bonded_usrp/ on both machines are
stale and can be deleted.

Regression after all changes (BPSK, 30 s): bonded TX → one-radio RX 20 MHz
78/79; 2 + 2 at 20/40/60 MHz 74/77, 76/76, 78/79; reverse direction (TX on
Machine 2, RX on Machine 1) at 40 MHz 79/79. 64-QAM through 2 + 2 at 20 MHz is
still 14/129 without the TX phase loop (RX pair phase drift, section 8).

Open hypothesis (not measured): the RX alignment freezes after ~10 ms. If
that window holds only uncorrelated noise in the overlap, the frozen phase is
arbitrary. Logging `alignment()` at the freeze would tell.

## 16. 2026-11-05: 64-QAM through 2 TX + 2 RX (branch gl/64qam)

Starting point: 64-QAM 2/3 through a bonded TX and a bonded RX at 20 MHz
decoded 14/129. Now 64-QAM 3/4 decodes 97–100 % at 20 and 40 MHz through
2 + 2 in both directions (table in EXPERIMENTS.md §5). Four separate causes:

**1. TX: the filter split cannot survive an arbitrary relative phase.** An
offline model (split → each radio's DAC → sum with phase φ → the real
gr-ieee802-11 decoder, plus per-subcarrier EVM) showed it plainly:
`band_splitter` gives −31 dB EVM at φ = 0 but −8 dB on subcarriers ±1 at
180° (64-QAM 3/4: 30/30 → 22/30). The receiver sees `g_A + e^{jφ} g_B`; the
two split filters only sum to an impulse at φ = 0, and each is ~130 taps,
far longer than the 16-sample CP, so any other φ is ISI around the seam.
Splitting each OFDM symbol in the subcarrier domain is exact at any φ, but a
naive per-symbol split loses it again as soon as the radio's interpolation
filter cuts the leakage of the sharp symbol edges (−20 dB). With an 8-sample
raised-cosine overlap between symbols the leakage stays near the band and
the split is clean: −38…−55 dB at any φ. Implemented as the new block
`ofdm_splitter`, which synthesizes each radio's subcarriers directly at the
radio's rate (one 64-point FFT + two 40-point IFFTs per symbol, no
resampling filter). Plan: 5/8·T per radio at fc ∓ 12/64·T, i.e. the RX plan.
It finds frames by the PHY's `packet_len` tag, so `wifi_bonded_tx.py` feeds it
straight from the PHY (no `packet_pad2`). Live, bonded TX → one radio:
64-QAM 3/4 89 % vs 74 % for the filter split in the first session, ~99 % once
the drive level was fixed (3 below).

Side finding: the relative phase of the two TX halves at the receiver changes
from frame to frame (−40°, 175°, 30°, −112° … in steps every ~1 s). The
splitters number their output samples, but the radios also transmit the
zero-fill in between, so each frame meets the LOs at a different phase.
Harmless with `ofdm_splitter`; with `band_splitter` it makes the seam
quality vary per frame (and is why the phase loop needed the PI mode).

**2. RX: the "pair drift 9–20 °/s" was float32 NCO increments.** Both
`overlap_reconstructor` and `band_splitter` rotated with a float phasor
advanced by a float increment, which runs at the increment's rounded
frequency. Predicted relative drift for the two radios (shifted in opposite
directions): 11.6 °/s at 20 MHz, 23 °/s at 40 MHz, matching the measured
9 and 20 °/s. Fixed with `detail::nco` (phase in double, exact start phase
every call). Measured afterwards over 100 s: −0.01 °/s.

**3. Drive levels.** `--tx-amplitude 0.6` put each radio at RMS 0.42 with
peaks up to 1.6, so ~0.25 % of the samples clipped at the DACs: EVM −22.5 dB
flat, 64-QAM 3/4 54/61. Amplitude 0.4 with TX gain 0.8: 62/62. On the RX
side, 40 dB saturated 30DBC3D (upper half of the channel −19 dB EVM vs −24
for the lower half, 0/78 decoded); 35 dB fixed it. Default amplitude is now
0.4 and the e2e scripts use RX 35 dB.

**4. RX: the right alignment phase depends on the emitter.** Each RX radio
has its own antenna, so the relative channel of the pair depends on where the
signal comes from. Logged live: the warmup froze on a weak continuous
coherent source in the overlap (coherence 0.34, phase 104°) while our frames
wanted ~145°. 40° only costs ~0.5 dB at the overlap centre, but a warmup on
pure noise gives a random phase (near 180° notches the seam). The fused path
now tracks the phase from `Σ a·b*` (gated by coherence, ~2 % CPU). First try,
forgetting 0.8 per call: it hopped between emitters inside frames and cost
20 points of 64-QAM (69/77 and 58/78). Final: an energy²-weighted sum that
never forgets, which locks onto the strongest signal (live: from −49° to our
frames' −15° within a few frames). A/B over 12 sessions: same decode rate
with and without tracking in this lab, where the warmup always saw something
coherent; unit tests cover the noise-warmup case.

**Also fixed: a 60 MHz stall in `bonded_source`.** With `BONDED_RX_PROFILE=1`:
recv 37.5 MS/s on both radios, served 0, rings at the 4 s cap, 2 fetches per
second. Once the rings are full, each push drops the oldest chunk; the fetch
chose its start time and extracted under separate locks, so a front moved in
between and every fetch began on a hole; and work() returned 0 on a degraded
block, after which GNU Radio retries a source only about twice a second. Fix:
start time and extraction under all ring locks, and retry inside work() until
`fetch_timeout`. 60 MHz at TX gain 0.9 went from 0 decoded to ~40 %.

**60 MHz remains marginal on Machine 2.** The stock PHY there does not keep
up with 60 MHz when the channel is busy (served 21–25 MS/s of 37.5 with the
TX at gain 0.9, whose LO leakage seems to add false detections), and 64-QAM
is SNR-limited (EVM ≈ −20 dB): 56/77 at TX 0.8, 69/78 at TX 0.85 + RX 38 dB.
BPSK is fine (77–79/80).

**Script crash on exit (fixed in `wifi_bonded_tx.py`).** Two runs ended with
`terminate ... Got unknown exception` after SIGINT. The handler called
`sys.exit()` while GR's waiter thread was still leaving C++; it now only
stops the flowgraph, main() reports after `tb.wait()`, and `faulthandler` is
on. No crash since in ~40 runs.

Tools added: `tx_frame_analyze.py --qam 16|64 --min-snr`, and
`tx_bonding_e2e_reverse.sh` (TX on Machine 2, RX here).

## 17. Retired documents and the 802.11 reference (2026-11-05)

When this repo was put under git (renamed from `bonded-test-tools` to
`gr-bonded-usrp-experiments`), these files were removed. Their history is
above; what they still had of value is here or in `EXPERIMENTS.md`.

- `OFFLINE_20MHZ_DECODE.md` (June): the record → offline reconstruct → decode
  validation of the RX at 20 MHz with the old 15 MS/s plan, written when live
  real time was out of reach. The procedure lives on as
  `EXPERIMENTS.md` §6 (record and replay), with the current plan.
- `SEMINAR_BRIEFING.md` (June): seminar study notes. Its pipeline
  descriptions and status are superseded (RX is real time up to 80 MHz, TX
  uses `ofdm_splitter`); its 802.11a/g parameter table is kept below.
- `TX_BONDING_LIMITATION.md`: status note on the August TX "limitation",
  superseded by § "TX bonding works (2026-11-03)" and §16.
- `BENCHMARK.md` and `benchmark/` (June): a stand-alone C++ harness for the
  reconstructor with the old 15 MS/s plan and OpenMP threads (both since
  abandoned); replaced by `bench/recon_bench.py`, which drives the real block.
- Scripts: `capture_sync.py` / `analyze_sync.py` (May, radio-to-radio sync
  captures, §1–3 era), `bonded_cw.py` / `cw_analyze.py` (June, TX phase
  measured with a third monitor radio; superseded by `tx_frame_analyze.py` on
  the receiver), `test_overlap_block.py` and `band_splitter/test_band_splitter.py`
  (smoke tests now covered by gr-bonded-usrp's `qa_*` tests), and all run logs.
  A tarball of the pre-cleanup tree (without the venv and test vectors) is at
  `~/bonded-uhd/bonded-test-tools-before-cleanup-20261105.tgz` on Machine 1.

### 802.11a/g PHY parameters

| Parameter | Value | Note |
|---|---|---|
| Standard | 802.11a/g (OFDM, "legacy") | not n/ac/ax — **no LDPC, no MIMO** |
| Channel bandwidth | 20 MHz | combined target |
| FFT size | 64 | |
| Subcarrier spacing | 312.5 kHz | 20 MHz / 64 |
| Used subcarriers | 52 | 48 data + 4 pilots |
| DC / null | subcarrier 0 nulled; guard at edges | seam lands here on TX |
| Pilot subcarriers | ±7, ±21 (indices 11, 25, 39, 53 after fftshift) | BPSK pilots |
| Cyclic prefix | 16 samples (0.8 µs) | = 1/4 of FFT, "long GI" |
| OFDM symbol | 80 samples = 4 µs | 64 + 16 |
| STF (short preamble) | 160 samples = 8 µs | 10× 16-sample reps |
| LTF (long preamble) | 160 samples = 8 µs | 2× 64 + 32 GI |
| Total preamble | 320 samples (`sync_length`) | STF+LTF |
| SIGNAL field | 1 OFDM symbol, BPSK r=1/2 (6 Mbps) | rate/length |
| Scrambler | 7-bit LFSR, S(x)=x⁷+x⁴+1 | randomizes data |
| FEC | convolutional **K=7**, generators (133,171)₈ | rate 1/2 mother code |
| Higher rates | puncturing → 2/3, 3/4 | |
| Interleaver | 2-pass block permutation (per OFDM symbol) | spreads burst errors |

**MCS / rate table (the "encoding" arg 0–7):**

| Idx | Modulation | Code rate | Bits/subcarrier | Data rate |
|---|---|---|---|---|
| 0 | BPSK | 1/2 | 1 | 6 Mbps |
| 1 | BPSK | 3/4 | 1 | 9 Mbps |
| 2 | QPSK | 1/2 | 2 | 12 Mbps |
| 3 | QPSK | 3/4 | 2 | 18 Mbps |
| 4 | 16-QAM | 1/2 | 4 | 24 Mbps |
| 5 | 16-QAM | 3/4 | 4 | 36 Mbps |
| 6 | 64-QAM | 2/3 | 6 | 48 Mbps |
| 7 | 64-QAM | 3/4 | 6 | 54 Mbps |

Constellation normalization: BPSK ±1; QPSK ±1/√2; 16-QAM ×√(1/10); 64-QAM ×√(1/42)
(unit average power). Source: `gr-ieee802-11/lib/constellations_impl.cc`.
