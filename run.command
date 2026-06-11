#!/usr/bin/env bash
# ============================================================
#  Frontline Hiring Scraper - double-click launcher (macOS)
#  Double-click this file in Finder. (First time, macOS may ask:
#  right-click -> Open -> Open to allow it.)
#  First run installs everything; later runs start in seconds.
# ============================================================
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo
  echo "Python 3 is not installed. Install Python 3.11+ from"
  echo "https://www.python.org/downloads/ then double-click this again."
  read -r -p "Press Enter to close."
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "Setting up for the first time (this takes a few minutes)..."
  python3 -m venv .venv
  source .venv/bin/activate
  python -m pip install --upgrade pip >/dev/null
  python -m pip install -r requirements.txt
  python -m playwright install chromium
  # Drop a Desktop alias so it can be launched from the Desktop next time.
  ln -sf "$(pwd)/run.command" "$HOME/Desktop/Frontline Hiring Scraper.command" 2>/dev/null || true
else
  source .venv/bin/activate
fi

echo
echo "Starting the Frontline Hiring Scraper..."
echo "Your browser will open at http://127.0.0.1:5050"
echo "Keep this window open while you use it. Press Ctrl+C to stop."
echo
( sleep 2; open http://127.0.0.1:5050 2>/dev/null || true ) &
python app.py
