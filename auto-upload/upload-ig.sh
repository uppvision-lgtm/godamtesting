#!/bin/bash
# Jalankan upload_ig.py dengan interpreter yang benar.
#
# Perlu pembungkus ini karena `python3` di PATH bukan interpreter yang punya
# playwright/pyautogui: paketnya ada di venv berikut.
#
# WAJIB dijalankan dari Terminal.app (bukan terminal VS Code) supaya klik
# level OS-nya diizinkan macOS.
set -euo pipefail
PY="/Users/mymac/proyek raksasa/instagram-bot-app/backend/.venv/bin/python3.14"
exec "$PY" "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/upload_ig.py" "$@"
