# ============================================================
# Docker image untuk backend Instagram Auto Comment Bot.
# Menjalankan FastAPI API + Celery worker dalam SATU kontainer.
# Platform-agnostik: Railway, Render, VPS Hostinger, dsb.
# ============================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# ffmpeg untuk menyusun video, fonts-dejavu untuk drawtext (teks di video).
# Tanpa font, filter drawtext gagal dengan "Cannot find a valid font".
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Salin requirements dulu (biar caching pip lebih efisien)
COPY requirements.txt ./

# Install dependency Python
RUN pip install --no-cache-dir -r requirements.txt

# Install browser Chromium beserta dependency sistem yang dibutuhkan Playwright
RUN playwright install --with-deps chromium

# Salin seluruh source backend (main.py, tasks.py, live_session.py, dll.)
COPY . .

# Beri izin eksekusi pada script start
RUN chmod +x start.sh

# Port yang dipakai container (Railway meng-inject $PORT saat runtime)
EXPOSE 8000

CMD ["./start.sh"]
