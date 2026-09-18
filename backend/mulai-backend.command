#!/bin/bash
# Nyalakan backend. KLIK DUA KALI dari Finder, atau jalankan dari Terminal.app.
#
# KENAPA HARUS LEWAT Terminal.app:
# Auto upload Instagram mengklik tombol Buat (+) dengan klik level OS
# (CGEventPost), karena Instagram diam-diam mengabaikan klik Playwright/CDP
# pada tombol itu - sudah diuji dengan locator.click, mouse down/up,
# focus+Enter, dan dispatch_event, keempatnya diabaikan. macOS hanya
# mengizinkan klik semacam itu untuk aplikasi yang punya izin Accessibility.
# Terminal.app punya izin itu, VS Code tidak. Proses yang lahir dari sini
# mewarisi izin Terminal, jadi tombol kirim di website bisa bekerja.
#
# Berkas .command sengaja dipakai (bukan .sh): klik dua kali di Finder
# membukanya lewat Terminal.app, jadi izinnya ikut dengan sendirinya.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "backend: $(pwd)"
echo "izin klik level OS:"
.venv/bin/python - <<'PY' || true
try:
    import pyautogui, time
    pyautogui.FAILSAFE = False
    awal = pyautogui.position()
    pyautogui.moveTo(awal.x + 40, awal.y + 40, 0.15)
    time.sleep(0.25)
    bisa = pyautogui.position() != awal
    pyautogui.moveTo(awal.x, awal.y, 0.15)
    print("  OK - upload Instagram bisa jalan" if bisa else
          "  BELUM ADA - upload Instagram akan gagal di tombol Buat (+).\n"
          "  Centang Terminal di System Settings -> Privacy & Security -> Accessibility,\n"
          "  lalu tutup dan buka lagi Terminal.")
except Exception as e:
    print(f"  tidak bisa diperiksa ({e})")
PY
echo

exec .venv/bin/uvicorn main:app --host :: --port 8000 --reload \
  --reload-dir . --reload-dir ../auto-upload/autoupload
