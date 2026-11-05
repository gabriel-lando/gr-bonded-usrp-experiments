# gr-bonded-usrp-experiments

Over-the-air 802.11 experiments, measurement tools and the lab log for
[gr-bonded-usrp](https://github.com/gabriel-lando/gr-bonded-usrp), the GNU Radio
module that bonds two Ettus B210 radios into one wider channel for receive and
transmit. What the blocks do and how to build them is documented there; this
repository is about using them on real radios and measuring the result.

## Documentation

- **[docs/EXPERIMENTS.md](docs/EXPERIMENTS.md)**: setup, and how to run every
  experiment (bonded TX, bonded RX, both, offline record and replay), with
  reference results and troubleshooting.
- [docs/LAB_NOTES.md](docs/LAB_NOTES.md): the engineering log: measurements,
  bugs found and design decisions, oldest first.
- [bench/README.md](bench/README.md): offline benchmarks of the
  reconstructor and of the 802.11 PHY, and the gr-ieee802-11 performance patch.

## Contents

| file | what it is |
|---|---|
| `wifi_bonded_rx.py` | bonded RX (two radios → one channel) + 802.11 receiver; live, record or replay |
| `wifi_bonded_tx.py` | 802.11 transmitter + bonded TX (one channel → two radios) |
| `wifi_trx.py` | single-radio 802.11 TX or RX (baseline and reference receiver) |
| `packet_rate_counter.py` | counts this testbed's frames in a receiver's output, packets/minute |
| `tx_bonding_e2e.sh` | two-machine run: TX here, RX on Machine 2 over ssh; prints sent vs decoded |
| `tx_bonding_e2e_reverse.sh` | the same with the TX on Machine 2 and the bonded RX here |
| `tx_frame_analyze.py` | per-frame analysis of an RX IQ capture: per-half channel, relative phase, EVM (per subcarrier) |
| `tx_phase_cal.py` | receiver-fed phase loop for the bonded TX with `--split filter` |
| `bench/` | offline benchmarks (no radios) and the gr-ieee802-11 patch |
| `setup_venv.sh`, `requirements.txt` | Python environment for the scripts |
| `install_gnuradio_build_deps.sh` | apt packages needed to build GNU Radio and the OOT modules from source |

## Quick start

```bash
./setup_venv.sh                 # once: .venv with access to the system GNU Radio modules
source .venv/bin/activate
./wifi_bonded_rx.py --help
```

Requires gr-bonded-usrp, gr-ieee802-11 and gr-foo installed system-wide, and
the `wifi_phy_hier` block generated from gr-ieee802-11 (docs/EXPERIMENTS.md §1).
The scripts default to the lab's radio serials and to Machine 2 as the remote
host (`rysen-s2c2-02`); every one of them takes the serials and hosts as
options or environment variables.

## License

GPL-3.0.
