#!/usr/bin/env bash
# Sets up this repo's pi_client to run on a Raspberry Pi with the Waveshare
# 10.85" e-Paper HAT+ (G) attached.
#
# !! OS REQUIREMENT: Raspberry Pi OS BOOKWORM (Debian 12) !!
#
# This panel does NOT work on Raspberry Pi OS Trixie (Debian 13). On Trixie
# the driver hangs forever in Init(), waiting on BUSY after command 0x04
# (POWER_ON) -- verified across two panels, two HAT+ boards, three Pi models,
# both the WiringPi and bcm2835 GPIO backends, and eight driver stacks. The
# identical hardware works immediately on Bookworm. See PANEL_ISSUE_NOTES.md.
#
# Run this FROM the pi_client/ directory, on the Pi itself:
#   cd pi_client
#   chmod +x install.sh
#   ./install.sh
#
# What it does:
#   1. Enables SPI (required for the display HAT)
#   2. Installs system packages (python3-venv, DejaVu fonts, git)
#   3. Creates a venv and installs Python dependencies
#   4. Downloads Waveshare's official demo package for this panel and vendors
#      its Python driver next to client.py. Uses the .zip from their wiki
#      rather than the GitHub repo -- their wiki recommends it, and the two
#      are not necessarily the same revision.
#   5. Copies config.example.yaml -> config.yaml if you don't have one yet
#   6. Installs + enables the systemd service so the dashboard starts on boot

set -euo pipefail
cd "$(dirname "$0")"

# Refuse to run on an OS this panel is known not to work on, rather than
# leaving you to rediscover it the hard way.
if [ -r /etc/os-release ]; then
  . /etc/os-release
  if [ "${VERSION_CODENAME:-}" != "bookworm" ]; then
    echo "WARNING: detected '${VERSION_CODENAME:-unknown}', not bookworm." >&2
    echo "The 10.85\" (G) panel does not initialise on Trixie -- Init() hangs" >&2
    echo "at POWER_ON. See PANEL_ISSUE_NOTES.md. Continuing anyway in 10s;" >&2
    echo "Ctrl+C to stop." >&2
    sleep 10
  fi
fi

echo "== 1/6: Enabling SPI =="
sudo raspi-config nonint do_spi 0

echo "== 2/6: Installing system packages =="
sudo apt update
sudo apt install -y python3-venv python3-pip fonts-dejavu-core git \
                    wget p7zip-full

echo "== 3/6: Creating virtualenv and installing Python deps =="
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
# Hardware-only deps for the display driver -- not needed for dry-run/dev.
# Raspberry Pi OS Bookworm moved to lgpio for GPIO access; older Bullseye
# images still use RPi.GPIO. Installed for other/future panel models that
# use them -- the 10.85" (G) panel itself doesn't: its driver instead loads
# a precompiled DEV_Config_*.so via ctypes (vendored below), independent of
# these.
./venv/bin/pip install spidev
./venv/bin/pip install RPi.GPIO || true
./venv/bin/pip install rpi-lgpio || true

echo "== 4/6: Vendoring the Waveshare e-Paper driver =="
if [ ! -d "waveshare_epd" ]; then
  # Waveshare's official demo package for this exact panel, from the product
  # wiki. Deliberately NOT the GitHub repo: their wiki recommends the zip, the
  # two are not necessarily the same revision, and the zip is what has been
  # verified working here.
  #
  # Note the package ships epd10in85g.py + epdconfig.py + four precompiled
  # DEV_Config_*.so variants. epdconfig.py picks the right .so at runtime from
  # `getconf LONG_BIT` (32/64) and whether /proc/cpuinfo says "Raspberry Pi 5"
  # (a "_w" suffix) or not (a "_b" suffix), searching its own directory first.
  # Vendor all four rather than guessing.
  zip_url="https://files.waveshare.com/wiki/10.85inch_e-Paper_HAT%2B_G/10.85inch_e-Paper_G.zip"

  # Unpacked under $HOME rather than /tmp -- on a Pi Zero, /tmp is often a
  # small tmpfs and this package can be big enough to fill it.
  tmp_dir=$(mktemp -d -p "$HOME")
  wget -q -O "$tmp_dir/panel.zip" "$zip_url"
  7z x -bso0 "$tmp_dir/panel.zip" -o"$tmp_dir/pkg"

  src_lib="$tmp_dir/pkg/RaspberryPi/python/lib"
  if [ ! -f "$src_lib/epd10in85g.py" ]; then
    echo "ERROR: expected $src_lib/epd10in85g.py in the downloaded package." >&2
    echo "Waveshare may have changed the package layout. Contents:" >&2
    find "$tmp_dir/pkg" -maxdepth 3 -type d >&2
    exit 1
  fi

  mkdir -p ./waveshare_epd
  cp "$src_lib/epd10in85g.py" "$src_lib/epdconfig.py" ./waveshare_epd/
  cp "$src_lib"/DEV_Config_*.so ./waveshare_epd/
  touch ./waveshare_epd/__init__.py
  rm -rf "$tmp_dir"
  echo "vendored waveshare_epd/ from Waveshare's official 10.85inch_e-Paper_G package"
else
  echo "waveshare_epd/ already present, skipping download"
fi

echo "== 5/6: Setting up config =="
if [ ! -f "config.yaml" ]; then
  cp config.example.yaml config.yaml
  echo "Created config.yaml from the example -- EDIT IT before starting the service:"
  echo "  nano config.yaml"
fi

echo "== 6/6: Installing systemd service =="
sudo cp systemd/eink-dashboard.service /etc/systemd/system/eink-dashboard.service
sudo sed -i "s#__PI_CLIENT_DIR__#$(pwd)#g" /etc/systemd/system/eink-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable eink-dashboard.service

cat <<EOF

Done.

Next steps:
  1. Edit config.yaml with your broker URL + token (nano config.yaml)
  2. Start the service:   sudo systemctl start eink-dashboard
  3. Watch the logs:      journalctl -u eink-dashboard -f
  4. Reboot to confirm it comes up on its own: sudo reboot

A reminder from Waveshare: avoid refreshing this panel more than roughly
once every 3 minutes, and let it refresh at least once every 24h. This is
already handled by client.py's default config -- see min_refresh_interval_seconds
and force_refresh_seconds in config.yaml if you want to change it.
EOF
