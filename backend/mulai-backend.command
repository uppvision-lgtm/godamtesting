#!/bin/bash
# Nyalakan backend. Klik dua kali dari Finder, atau jalankan dari terminal
# mana pun.
#
# Berkas .command dipertahankan supaya bisa dinyalakan tanpa mengetik perintah.
# Dulu ia WAJIB dijalankan dari Terminal.app karena upload Instagram memakai
# klik tingkat sistem yang butuh izin Accessibility macOS. Itu sudah tidak
# berlaku: Instagram kini diketuk dengan event sentuh, berjalan headless, dan
# tidak menyentuh kursor sama sekali.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "backend: $(pwd)"
echo

exec .venv/bin/uvicorn main:app --host :: --port 8000 --reload \
  --reload-dir . --reload-dir ../auto-upload/autoupload
