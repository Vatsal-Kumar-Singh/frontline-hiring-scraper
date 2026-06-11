#!/usr/bin/env bash
# ============================================================
#  Frontline Hiring Scraper - one-click launcher (Mac / Linux)
#  In Terminal:  ./run.sh      (first: chmod +x run.sh)
#  First run installs everything (a few minutes); later runs
#  start in seconds.
# ============================================================
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo
  echo "Python 3 is not installed. Install Python 3.11+ from"
  echo "https://www.python.org/downloads/ then run ./run.sh again."
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "Setting up for the first time (this takes a few minutes)..."
  python3 -m venv .venv
  source .venv/bin/activate
  python -m pip install --upgrade pip >/dev/null
  python -m pip install -r requirements.txt
  python -m playwright install chromium
else
  source .venv/bin/activate
fi

echo
echo "Starting the Frontline Hiring Scraper..."
echo "Open http://127.0.0.1:5050 in your browser."
echo "Keep this window open while you use it. Press Ctrl+C to stop."
echo
( sleep 2; (command -v open >/dev/null && open http://127.0.0.1:5050) || (command -v xdg-open >/dev/null && xdg-open http://127.0.0.1:5050) || true ) &
python app.py
