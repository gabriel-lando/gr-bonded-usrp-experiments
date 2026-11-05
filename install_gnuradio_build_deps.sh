#!/usr/bin/env bash
set -euo pipefail

# Install every apt dependency needed for a *full* `cmake .. ` build of
# GNU Radio 3.10 from source on Ubuntu 24.04 (noble) with ALL components
# enabled (not just the minimal gr-uhd/gr-blocks/gr-digital set this
# project actually needs).
#
# Derived by mapping every "not found" / "disabled" line in a `cmake ..`
# configure log (run 2026-08-24 on rysen-s2c2-01) to its Ubuntu package name,
# and verifying each package exists via `apt-cache show`. UHD itself is
# assumed already built/installed separately (this project builds it from
# source, not from apt).
#
# Usage:
#   ./install_gnuradio_build_deps.sh
# then, in the gnuradio build dir:
#   cmake .. && make -j"$(nproc)" && sudo make install && sudo ldconfig

sudo apt-get update

sudo apt-get install -y \
  `# --- python bindings (enables python-support) ---` \
  python3-numpy \
  python3-packaging \
  pybind11-dev \
  python3-pybind11 \
  python3-pygccxml \
  `# --- VOLK + pkg-config (enables VOLK support, gates gnuradio-runtime) ---` \
  pkg-config \
  libvolk-dev \
  `# --- math libs (GMP/MPFR for perf counters; spdlog+FFTW3f+GSL gate gnuradio-runtime / gr-fft / gr-fec / gr-wavelet) ---` \
  libgmp-dev \
  libmpfr-dev \
  libspdlog-dev \
  libfftw3-dev \
  libgsl-dev \
  `# --- gr-blocks (wav sink/source) ---` \
  libsndfile1-dev \
  `# --- gr-qtgui (Qt5 + Qwt + PyQt5 GUI blocks) ---` \
  qtbase5-dev \
  libqwt-qt5-dev \
  python3-pyqt5 \
  `# --- gr-ctrlport (Thrift-based remote control, optional/heavy) ---` \
  libthrift-dev \
  thrift-compiler \
  `# --- gr-iio (Analog Devices / PlutoSDR support, not needed for B210 but part of a full build) ---` \
  libiio-dev \
  libad9361-dev \
  `# --- gr-zeromq ---` \
  libzmq3-dev \
  `# --- gr-soapy (SoapySDR abstraction layer) ---` \
  libsoapysdr-dev \
  soapysdr-tools \
  `# --- gr-video-sdl (legacy SDL1.2 video sink) ---` \
  libsdl1.2-dev \
  `# --- gnuradio-companion (GRC GUI): needs GObject-Introspection typelibs,` \
  `#     not just python3-gi itself (PYGI_FOUND can be TRUE while these are missing) ---` \
  gir1.2-gtk-3.0 \
  python3-gi-cairo \
  gir1.2-pango-1.0

echo ""
echo "Done. Re-run cmake — every previously-disabled component above should"
echo "now show as enabled in the 'Gnuradio enabled components' summary."
echo "(UHD itself is not installed by this script — build/install it separately"
echo "if 'Found UHD' doesn't already appear in the cmake output.)"
