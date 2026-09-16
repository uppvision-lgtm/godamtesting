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
# concurrency-nya — render banyak template jadi selalu berurutan. Prefork
# menjalankan sebanyak CELERY_CONCURRENCY proses sekaligus.
celery -A celery_app.celery_app worker --loglevel="$LOG_LEVEL" \
  --pool=prefork --concurrency="$CELERY_CONCURRENCY" &
CELERY_PID=$!

echo "==> Menjalankan FastAPI di port $PORT ..."
uvicorn main:app --host 0.0.0.0 --port "$PORT" &
UVICORN_PID=$!

# Saat salah satu proses berhenti, hentikan juga proses lainnya
trap 'kill "$CELERY_PID" "$UVICORN_PID" 2>/dev/null || true' EXIT
wait -n "$CELERY_PID" "$UVICORN_PID"
