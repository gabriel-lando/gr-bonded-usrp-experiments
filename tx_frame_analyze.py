#!/usr/bin/env python3
#
# Offline per-frame diagnosis of a raw RX IQ capture (wifi_trx.py --iq-capture)
# of 802.11a/g frames — built to debug the bonded (hard-split) TX path.
#
# For every burst found in the capture it:
#   1. locates the LTF by cross-correlation (fine timing),
#   2. removes CFO (coarse from STF lag-16, fine from LTF lag-64),
#   3. estimates the per-subcarrier channel H[k] from the two LTF symbols,
#   4. equalizes every following OFDM symbol (common phase tracked on the
#      4 pilots) and measures BPSK EVM per subcarrier.
#
# What it reports per frame (lower half = k<0 = radio A, upper = k>0 = radio B):
#   |H| per half, relative phase between halves (each half's linear phase fit
#   extrapolated to k=0), per-half group delay (phase slope, in samples), the
#   LTF matched-filter peak relative to the "coherent" peak, data EVM per
#   half, and "drift": how far the upper half's pilot phase wanders against
#   the lower half's within the frame (deg).  Drift is what a receiver's single
#   common-phase tracker cannot follow; with only 2 pilots per half it unwraps
#   into nonsense at low SNR or on foreign bursts (thousands of degrees = a
#   measurement failure, not a real drift).
#   A single-radio TX gives one smooth line: rel phase ~0, equal delay.
#
# This is a test/diagnostic tool only (offline, numpy) — none of it runs in
# the product TX/RX path.
#
# Usage:
#   ./tx_frame_analyze.py capture.fc32 --rate 10e6 [--start 10 --dur 5]
#   (BPSK data assumed for EVM, i.e. --encoding 0 on the TX side; --qam 16/64
#   for the QAM encodings)

import argparse
import numpy as np

# 802.11a LTF / STF in frequency domain, k = -26..26
LTF = np.array([1, 1, -1, -1, 1, 1, -1, 1, -1, 1, 1, 1, 1, 1, 1, -1, -1, 1, 1, -1,
                1, -1, 1, 1, 1, 1, 0, 1, -1, -1, 1, 1, -1, 1, -1, 1, -1, -1, -1, -1,
                -1, 1, 1, -1, -1, 1, -1, 1, -1, 1, 1, 1, 1], dtype=complex)
KS = np.arange(-26, 27)
PILOT_K = np.array([-21, -7, 7, 21])
PILOT_V = np.array([1, 1, 1, -1], dtype=complex)
POLARITY = np.array([
    1, 1, 1, 1, -1, -1, -1, 1, -1, -1, -1, -1, 1, 1, -1, 1, -1, -1, 1, 1, -1, 1, 1, -1, 1, 1, 1, 1,
    1, 1, -1, 1, 1, 1, -1, 1, 1, -1, -1, 1, 1, 1, -1, 1, -1, -1, -1, 1, -1, 1, -1, -1, 1, -1, -1, 1,
    1, 1, 1, 1, -1, -1, 1, 1, -1, -1, 1, -1, 1, -1, 1, 1, -1, -1, -1, 1, 1, -1, -1, -1, -1, 1, -1, -1,
    1, -1, 1, 1, 1, 1, -1, 1, -1, 1, -1, 1, -1, -1, -1, -1, -1, 1, -1, 1, 1, -1, 1, -1, 1, 1, 1, -1,
    -1, 1, -1, -1, -1, 1, 1, 1, -1, -1, -1, -1, -1, -1, -1])
DATA_K = np.array([k for k in range(-26, 27) if k != 0 and k not in PILOT_K])


def bins(ks):
    return np.asarray(ks) % 64


def ltf_time():
    X = np.zeros(64, complex)
    X[bins(KS)] = LTF
    return np.fft.ifft(X) * 8.0  # unit-ish power


def find_bursts(x, rate, thr_db, min_len_s):
    blk = 80
    nb = len(x) // blk
    p = (np.abs(x[:nb * blk].reshape(nb, blk)) ** 2).mean(axis=1)
    floor = np.median(p)
    on = p > floor * 10 ** (thr_db / 10)
    bursts = []
    i = 0
    min_blocks = int(min_len_s * rate / blk)
    while i < nb:
        if on[i]:
            j = i
            gap = 0
            while j < nb and gap < 4:
                gap = 0 if on[j] else gap + 1
                j += 1
            if j - i >= min_blocks:
                bursts.append(((i - 2) * blk, (j - 4) * blk))
            i = j
        else:
            i += 1
    return bursts, floor


def lin_fit_phase(k, h):
    """Fit angle(h) = a + b*k (unwrapped). Returns a (rad at k=0), b."""
    ph = np.unwrap(np.angle(h))
    b, a = np.polyfit(k, ph, 1)
    return a, b


def analyze_frame(x, s0, s1, lt, nsym_max, qam=0):
    seg = x[max(0, s0):s1]
    # fine timing: LTF matched filter over the first 600 samples
    search = seg[:700]
    c = np.abs(np.correlate(search, lt, mode='valid'))
    # two LTF symbols 64 apart: take max of c[n]+c[n+64]
    cc = c[:-64] + c[64:]
    n1 = int(np.argmax(cc))
    pk = cc[n1]
    # coarse CFO from STF (lag 16) on the 120 samples before LTF CP
    stf = seg[max(0, n1 - 32 - 140):n1 - 32 - 10]
    cfo = np.angle(np.sum(stf[16:] * np.conj(stf[:-16]))) / 16 if len(stf) > 32 else 0.0
    n = np.arange(len(seg))
    seg = seg * np.exp(-1j * cfo * n)
    l1 = seg[n1:n1 + 64]
    l2 = seg[n1 + 64:n1 + 128]
    cfo2 = np.angle(np.sum(l2 * np.conj(l1))) / 64
    seg = seg * np.exp(-1j * cfo2 * n)
    l1 = seg[n1:n1 + 64]
    l2 = seg[n1 + 64:n1 + 128]
    Y = (np.fft.fft(l1) + np.fft.fft(l2)) / 2
    H = Y[bins(KS)] / np.where(LTF == 0, 1, LTF)
    H[26] = np.nan
    lo = KS < 0
    hi = KS > 0
    # ignore the 2 subcarriers adjacent to the seam for the linear fit
    lo_fit = (KS <= -3)
    hi_fit = (KS >= 3)
    a_lo, b_lo = lin_fit_phase(KS[lo_fit], H[lo_fit])
    a_hi, b_hi = lin_fit_phase(KS[hi_fit], H[hi_fit])
    rel = np.angle(np.exp(1j * (a_hi - a_lo)))
    # "coherent" reference peak = what the peak would be if both halves had
    # the same complex gain: sum |H| instead of |sum H|
    coh = np.nansum(np.abs(H))
    actual = np.abs(np.nansum(H))
    # data symbols: start after LTF (n1+128), each 80 samples, FFT on last 64
    evm_k = np.zeros(53)
    evm_sym = []
    pil_lo, pil_hi = [], []
    cnt = 0
    p0 = n1 + 128
    nsym = min(nsym_max, (len(seg) - p0) // 80)
    Hd = H.copy()
    Hd[26] = 1
    for s in range(nsym):
        sym = seg[p0 + s * 80 + 16:p0 + s * 80 + 80]
        if len(sym) < 64:
            break
        Z = np.fft.fft(sym)[bins(KS)] / Hd
        pil = Z[PILOT_K + 26] * np.conj(PILOT_V * POLARITY[s % 127])
        pil_lo.append(np.angle(pil[0] + pil[1]))
        pil_hi.append(np.angle(pil[2] + pil[3]))
        Z = Z * np.exp(-1j * np.angle(np.sum(pil)))
        d = Z[DATA_K + 26]
        if qam and s == 0:
            continue  # SIGNAL field is always BPSK
        if qam:
            # Square QAM decision: normalize to the constellation's average
            # power, snap each axis to the nearest odd level.
            m = int(round(np.sqrt(qam)))
            es = 2.0 * (qam - 1) / 3.0
            g = d / (np.sqrt(np.mean(np.abs(d) ** 2)) + 1e-20) * np.sqrt(es)
            dec = lambda v: np.clip(2 * np.floor(v / 2) + 1, -(m - 1), m - 1)
            err = np.abs(g - (dec(g.real) + 1j * dec(g.imag))) ** 2 / es
        else:
            # BPSK (SIGNAL + encoding 0 data): decision on real axis.
            # Normalize per symbol first: B210 burst starts ramp up in power
            # for ~300 us, so the LTF-based |H| is too small for later symbols.
            d = d / (np.mean(np.abs(d.real)) + 1e-20)
            err = np.abs(d - np.sign(d.real)) ** 2
        evm_sym.append(np.mean(err))
        evm_k[DATA_K + 26] += err
        cnt += 1
    if cnt:
        evm_k /= cnt
    # Phase of the upper half's pilots relative to the lower half's, across
    # the frame: a single-radio TX/RX keeps this flat. Its total swing is
    # what a receiver's single common-phase tracker cannot follow.
    if len(pil_lo) > 2:
        dd = np.unwrap(np.array(pil_hi)) - np.unwrap(np.array(pil_lo))
        drift_deg = np.degrees(np.max(dd) - np.min(dd))
    else:
        drift_deg = np.nan
    evm_lo = 10 * np.log10(np.mean(evm_k[(DATA_K[DATA_K < 0]) + 26]) + 1e-12)
    evm_hi = 10 * np.log10(np.mean(evm_k[(DATA_K[DATA_K > 0]) + 26]) + 1e-12)
    return dict(
        n1=n1, pk=pk, cfo_hz=(cfo + cfo2), H=H, evm_k=evm_k, nsym=cnt, evm_sym=np.array(evm_sym),
        mag_lo=np.nanmean(np.abs(H[lo])), mag_hi=np.nanmean(np.abs(H[hi])),
        rel_deg=np.degrees(rel), d_lo=-b_lo * 64 / (2 * np.pi), d_hi=-b_hi * 64 / (2 * np.pi),
        coh_ratio=actual / coh, evm_lo=evm_lo, evm_hi=evm_hi, drift_deg=drift_deg)


def main():
    ap = argparse.ArgumentParser(description='per-frame 802.11 LTF/EVM diagnosis')
    ap.add_argument('capture')
    ap.add_argument('--rate', type=float, default=10e6)
    ap.add_argument('--start', type=float, default=5.0, help='seconds into capture')
    ap.add_argument('--dur', type=float, default=5.0, help='seconds to analyze')
    ap.add_argument('--thr-db', type=float, default=3.0)
    ap.add_argument('--min-len', type=float, default=0.1e-3, help='min burst length (s); 64QAM 500 B frames are ~0.28 ms at 10 MHz, ~0.14 ms at 20 MHz')
    ap.add_argument('--nsym', type=int, default=200)
    ap.add_argument('--per-k', action='store_true', help='print per-subcarrier H/EVM of the mean')
    ap.add_argument('--per-sym', action='store_true',
                    help='also print each frame\'s EVM along the frame (dB per 10-symbol group): '
                         'shows where a frame goes bad (a mid-frame sample slip vs bad from the start)')
    ap.add_argument('--tail', type=float, default=None,
                    help='analyze the LAST this-many seconds (overrides --start/--dur; '
                         'works on a capture that is still being written)')
    ap.add_argument('--qam', type=int, default=0, choices=(0, 4, 16, 64),
                    help='data constellation for the EVM decision (0 = BPSK, the default)')
    ap.add_argument('--min-snr', type=float, default=None,
                    help='only list (and use for --per-k) bursts at least this SNR (dB): '
                         'keeps other 802.11 traffic on the channel out of the statistics')
    ap.add_argument('--summary', action='store_true',
                    help='print only "REL <circular-mean rel_deg> <frames> <evm_lo> <evm_hi>"')
    args = ap.parse_args()

    x = np.memmap(args.capture, dtype=np.complex64, mode='r')
    if args.tail is not None:
        args.dur = args.tail
        args.start = max(0.0, len(x) / args.rate - args.tail)
    a = int(args.start * args.rate)
    b = min(len(x), a + int(args.dur * args.rate))
    xs = np.asarray(x[a:b]).astype(complex)
    bursts, floor = find_bursts(xs, args.rate, args.thr_db, args.min_len)
    if args.summary:
        # Keep only our own frames: other 802.11 traffic on the channel is
        # weaker here (the TX radios sit next to the RX), so take bursts
        # within 6 dB of the strongest one in the window.
        cand = []
        for s0, s1 in bursts:
            if s0 < 0 or s1 - s0 < 2000:  # 64QAM frames are ~2800 samples
                continue
            p = np.mean(np.abs(xs[s0 + 400:s1 - 100]) ** 2)
            cand.append((10 * np.log10(p / floor), s0, s1))
        top = max((c[0] for c in cand), default=0.0)
        rows = [analyze_frame(xs, s0, s1, ltf_time(), args.nsym, args.qam)
                for snr, s0, s1 in cand if snr >= max(10.0, top - 6.0)]
        if not rows:
            print("REL nan 0 nan nan")
            return
        rel = np.degrees(np.angle(np.mean(np.exp(1j * np.radians([r['rel_deg'] for r in rows])))))
        print(f"REL {rel:.1f} {len(rows)} {np.median([r['evm_lo'] for r in rows]):.1f} "
              f"{np.median([r['evm_hi'] for r in rows]):.1f}")
        return
    print(f"# {len(bursts)} bursts in [{args.start:.1f}, {args.start + args.dur:.1f}] s, "
          f"noise floor {10 * np.log10(floor):.1f} dB")
    lt = ltf_time()
    rows = []
    print(f"{'t_s':>8} {'len':>6} {'snr':>5} {'cfo_Hz':>8} {'|Hlo|':>6} {'|Hhi|':>6} "
          f"{'rel_deg':>8} {'d_lo':>6} {'d_hi':>6} {'coh':>5} {'evm_lo':>7} {'evm_hi':>7} {'drift':>6}")
    for s0, s1 in bursts:
        if s0 < 0 or s1 - s0 < 1000:
            continue
        p = np.mean(np.abs(xs[s0 + 400:s1 - 100]) ** 2)
        snr = 10 * np.log10(p / floor)
        if args.min_snr is not None and snr < args.min_snr:
            continue
        r = analyze_frame(xs, s0, s1, lt, args.nsym, args.qam)
        rows.append(r)
        print(f"{(a + s0) / args.rate:8.3f} {s1 - s0:6d} {snr:5.1f} {r['cfo_hz'] * args.rate / (2 * np.pi):8.0f} "
              f"{r['mag_lo']:6.3f} {r['mag_hi']:6.3f} {r['rel_deg']:8.1f} {r['d_lo']:6.2f} {r['d_hi']:6.2f} "
              f"{r['coh_ratio']:5.2f} {r['evm_lo']:7.1f} {r['evm_hi']:7.1f} {r['drift_deg']:6.0f}")
        if args.per_sym and len(r['evm_sym']) >= 10:
            e = r['evm_sym'][:len(r['evm_sym']) // 10 * 10].reshape(-1, 10).mean(axis=1)
            print("          per-10-sym EVM dB: " + " ".join(f"{10 * np.log10(v + 1e-12):.0f}" for v in e))
    if rows and args.per_k:
        # median across frames: one stray burst (ambient WiFi, a cut frame)
        # must not dominate the per-subcarrier picture
        evm = np.median([r['evm_k'] for r in rows], axis=0)
        Hm = np.nanmedian([np.abs(r['H']) / np.nanmean(np.abs(r['H'])) for r in rows], axis=0)
        print("# k  |H|norm  evm_dB")
        for i, k in enumerate(KS):
            if k == 0:
                continue
            print(f"{k:4d} {Hm[i]:7.3f} {10 * np.log10(evm[i] + 1e-12) if evm[i] > 0 else float('nan'):7.1f}")


if __name__ == '__main__':
    main()
