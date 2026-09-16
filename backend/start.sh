#!/usr/bin/env bash
# ============================================================
# Entrypoint container untuk backend.
# Menjalankan FastAPI (uvicorn) DAN Celery worker sekaligus.
# ============================================================
set -euo pipefail

# Default aman untuk server (bisa di-override lewat Environment Variable)
export LIVE_HEADLESS="${LIVE_HEADLESS:-true}"
export COMMENT_STATE_DIR="${COMMENT_STATE_DIR:-/tmp/comment-state}"
export CELERY_CONCURRENCY="${CELERY_CONCURRENCY:-1}"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"

# Railway / Render / dsb. meng-inject variabel PORT otomatis.
# Default 8000 dipakai kalau variabel PORT tidak ada.
PORT="${PORT:-8000}"

echo "==> Menjalankan Celery worker..."
# Pool "solo" hanya mengerjakan satu tugas pada satu waktu, berapa pun
# concurrency-nya, sehingga render banyak template selalu berurutan.
#
# Dipakai "threads", bukan "prefork": prefork menyalin seluruh proses Python
# untuk tiap worker, dan di container kecil sisa memorinya jadi tidak cukup
# untuk ffmpeg — rendernya dimatikan sistem. Threads berbagi satu proses,
# dan itu tidak memperlambat apa pun di sini karena kerja beratnya ada di
# ffmpeg yang berjalan sebagai proses terpisah.
celery -A celery_app.celery_app worker --loglevel="$LOG_LEVEL" \
  --pool=threads --concurrency="$CELERY_CONCURRENCY" &
CELERY_PID=$!

echo "==> Menjalankan FastAPI di port $PORT ..."
uvicorn main:app --host 0.0.0.0 --port "$PORT" &
UVICORN_PID=$!

# Saat salah satu proses berhenti, hentikan juga proses lainnya
trap 'kill "$CELERY_PID" "$UVICORN_PID" 2>/dev/null || true' EXIT
wait -n "$CELERY_PID" "$UVICORN_PID"
