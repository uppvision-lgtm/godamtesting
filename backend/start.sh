#!/usr/bin/env bash
# ============================================================
# Entrypoint container untuk backend.
#
# Satu berkas, tiga peran, dipilih lewat environment ROLE:
#
#   ROLE=all     (bawaan) FastAPI + Celery worker dalam satu container.
#   ROLE=api     hanya FastAPI. Dipakai kalau worker dipisah.
#   ROLE=worker  hanya Celery worker.
#
# Memisah API dan worker membuat render berat tidak lagi memperlambat
# halaman, dan keduanya bisa diberi ukuran container yang berbeda. Itu baru
# aman sejak status job pindah ke Redis (lihat video_tasks.py); selama status
# masih berupa berkas, keduanya wajib berbagi disk.
# ============================================================
set -euo pipefail

ROLE="${ROLE:-all}"

# Default aman untuk server (bisa di-override lewat Environment Variable)
export LIVE_HEADLESS="${LIVE_HEADLESS:-true}"
export COMMENT_STATE_DIR="${COMMENT_STATE_DIR:-/tmp/comment-state}"
# Berapa video dirender BERSAMAAN. Tidak ada batas atas di kode; angkanya
# semata-mata ditentukan di sini. Bawaannya 1 karena container kecil kehabisan
# memori begitu ada dua ffmpeg sekaligus. Di server sendiri yang kuat, isi
# sebanyak inti yang mau dipakai.
export CELERY_CONCURRENCY="${CELERY_CONCURRENCY:-1}"
# "threads" cocok karena kerja beratnya ada di ffmpeg sebagai proses terpisah,
# jadi tidak terganjal GIL. "prefork" tersedia kalau suatu saat dibutuhkan.
export CELERY_POOL="${CELERY_POOL:-threads}"
export LOG_LEVEL="${LOG_LEVEL:-WARNING}"

# Railway / Render / dsb. meng-inject variabel PORT otomatis.
# Default 8000 dipakai kalau variabel PORT tidak ada.
PORT="${PORT:-8000}"

jalankan_worker() {
  # Worker tidak pernah membuka daftar template; ia hanya menarik berkas yang
  # benar-benar dipakai. Menyinkronkan seluruh daftar dari Supabase di sini
  # cuma menambah permintaan tanpa guna.
  export TEMPLATE_SYNC="${TEMPLATE_SYNC:-off}"
  echo "==> Menjalankan Celery worker..."
  # Dipakai "threads", bukan "prefork": prefork menyalin seluruh proses Python
  # untuk tiap worker, dan di container kecil sisa memorinya jadi tidak cukup
  # untuk ffmpeg — rendernya dimatikan sistem. Threads berbagi satu proses,
  # dan itu tidak memperlambat apa pun di sini karena kerja beratnya ada di
  # ffmpeg yang berjalan sebagai proses terpisah.
  celery -A celery_app.celery_app worker --loglevel="$LOG_LEVEL" \
    --pool="$CELERY_POOL" --concurrency="$CELERY_CONCURRENCY"
}

jalankan_api() {
  echo "==> Menjalankan FastAPI di port $PORT ..."
  uvicorn main:app --host 0.0.0.0 --port "$PORT"
}

case "$ROLE" in
  worker)
    # Railway memeriksa kesehatan lewat HTTP, jadi worker pun perlu menjawab
    # di $PORT. Penjawabnya sengaja sangat kecil (health_worker.py).
    jalankan_worker &
    CELERY_PID=$!
    trap 'kill "$CELERY_PID" 2>/dev/null || true' EXIT
    echo "==> Penjawab kesehatan worker di port $PORT ..."
    python health_worker.py &
    HEALTH_PID=$!
    trap 'kill "$CELERY_PID" "$HEALTH_PID" 2>/dev/null || true' EXIT
    if wait -n "$CELERY_PID" "$HEALTH_PID" 2>/dev/null; then
      :
    else
      status=$?
      if [ "$status" -gt 1 ] 2>/dev/null; then
        wait "$CELERY_PID" "$HEALTH_PID" || true
      fi
    fi
    ;;
  api)
    exec bash -c "$(declare -f jalankan_api); jalankan_api"
    ;;
  all)
    jalankan_worker &
    CELERY_PID=$!
    jalankan_api &
    UVICORN_PID=$!
    # Saat salah satu proses berhenti, hentikan juga proses lainnya.
    trap 'kill "$CELERY_PID" "$UVICORN_PID" 2>/dev/null || true' EXIT
    # "wait -n" butuh bash 4.3+. Container memakai Debian (bash 5), tetapi
    # macOS masih bash 3.2, jadi ada jalur cadangan supaya skrip ini tidak
    # mati seketika saat dicoba di laptop.
    if wait -n "$CELERY_PID" "$UVICORN_PID" 2>/dev/null; then
      :
    else
      status=$?
      if [ "$status" -gt 1 ] 2>/dev/null; then
        wait "$CELERY_PID" "$UVICORN_PID" || true
      fi
    fi
    ;;
  *)
    echo "ROLE tidak dikenal: $ROLE (pakai all, api, atau worker)" >&2
    exit 64
    ;;
esac
