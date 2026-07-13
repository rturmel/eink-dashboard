#!/usr/bin/env bash
# Sets up this repo's pi_client to run on a Raspberry Pi (tested target:
# Raspberry Pi Zero WH, Raspberry Pi OS Bookworm) with the Waveshare
# 10.85" e-Paper HAT+ (G) attached.
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
#   4. Clones Waveshare's official driver repo and vendors the
#      epd10in85g module + its dependencies next to client.py
#   5. Copies config.example.yaml -> config.yaml if you don't have one yet
#   6. Installs + enables the systemd service so the dashboard starts on boot

set -euo pipefail
cd "$(dirname "$0")"

echo "== 1/6: Enabling SPI =="
sudo raspi-config nonint do_spi 0

echo "== 2/6: Installing system packages =="
sudo apt update
sudo apt install -y python3-venv python3-pip fonts-dejavu-core git

echo "== 3/6: Creating virtualenv and installing Python deps =="
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
# Hardware-only deps for the display driver -- not needed for dry-run/dev.
# Raspberry Pi OS Bookworm moved to lgpio for GPIO access; older Bullseye
# images still use RPi.GPIO. Try both so this works either way; the
# waveshare_epd driver picks whichever is available at import time.
./venv/bin/pip install spidev
./venv/bin/pip install RPi.GPIO || true
./venv/bin/pip install rpi-lgpio || true

echo "== 4/6: Vendoring the Waveshare e-Paper driver =="
if [ ! -d "waveshare_epd" ]; then
  tmp_dir=$(mktemp -d)
  git clone --depth 1 https://github.com/waveshare/e-Paper.git "$tmp_dir/e-Paper"
  cp -r "$tmp_dir/e-Paper/RaspberryPi_JetsonNano/python/lib/waveshare_epd" ./waveshare_epd
  rm -rf "$tmp_dir"
  echo "vendored waveshare_epd/ next to client.py"
else
  echo "waveshare_epd/ already present, skipping clone"
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
