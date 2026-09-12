import asyncio
import hashlib
import os
import sys
from datetime import datetime, timezone

# Playwright butuh subprocess. Di Windows, loop default uvicorn (Selector)
# tidak mengimplementasikannya dan melempar NotImplementedError kosong.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import redis
from celery.result import AsyncResult
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from celery_app import celery_app
from comment_ai import normalize_tone
from tasks import run_instagram_bot
from live_session import router as live_login_router
from presence import router as presence_router
from video_api import router as video_router
from upload_api import router as upload_router
from auth_api import router as auth_router


app = FastAPI(title="Instagram Auto Comment Bot API")
rate_limit_store = redis.Redis.from_url(
    os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    decode_responses=True,
    socket_timeout=2,
)
rate_limit_per_day = int(os.getenv("RATE_LIMIT_PER_IP_PER_DAY", "10"))

frontend_origins = [
    origin.strip()
    for origin in os.getenv(
        "FRONTEND_URL", "http://localhost:3000,http://127.0.0.1:3000"
    ).split(",")
    if origin.strip()
]

# Saat backend lokal (DEBUG=true) izinkan semua origin, supaya mode "via PC"
# (browser membuka frontend dari domain Vercel lalu memanggil localhost:8000)
# tidak diblokir CORS. Railway tetap memakai daftar FRONTEND_URL di atas.
local_debug = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if local_debug else frontend_origins,
    allow_credentials=False if local_debug else True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(live_login_router)
app.include_router(presence_router)
app.include_router(video_router)
app.include_router(upload_router)
app.include_router(auth_router)


class JobRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    target: str = Field(min_length=1, max_length=200)
    comment_count: int = Field(ge=1, le=100)
    session_id: str = Field(min_length=1, max_length=2000)
    # Nada komentar yang dipilih user di form: positif (default) | netral | negatif
    tone: str = "positif"


def enforce_rate_limit(request: Request) -> None:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    client_ip = forwarded_for.split(",")[0].strip() or (request.client.host if request.client else "unknown")
    day = datetime.now(timezone.utc).date().isoformat()
    key = f"rate-limit:start-job:{day}:{hashlib.sha256(client_ip.encode()).hexdigest()}"
    try:
        request_count = rate_limit_store.incr(key)
        if request_count == 1:
            rate_limit_store.expire(key, 86400)
    except redis.RedisError as error:
        raise HTTPException(status_code=503, detail="Rate limit service unavailable") from error
    if request_count > rate_limit_per_day:
        raise HTTPException(status_code=429, detail="Daily request limit exceeded")


def encrypt_secret(secret: str) -> str:
    encryption_key = os.getenv("CREDENTIAL_ENCRYPTION_KEY")
    if not encryption_key:
        raise HTTPException(status_code=503, detail="Credential encryption is not configured")
    try:
        return Fernet(encryption_key.encode()).encrypt(secret.encode()).decode()
    except ValueError as error:
        raise HTTPException(status_code=503, detail="Credential encryption key is invalid") from error


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/start-job")
def start_job(request: Request, job: JobRequest) -> dict[str, str]:
    enforce_rate_limit(request)
    encrypted_session_id = encrypt_secret(job.session_id)
    task = run_instagram_bot.delay(
        job.username,
        job.target,
        job.comment_count,
        encrypted_session_id,
        normalize_tone(job.tone),
    )
    return {"task_id": task.id, "status": "submitted"}


@app.get("/api/job-status/{task_id}")
def get_job_status(task_id: str) -> dict:
    result = AsyncResult(task_id, app=celery_app)
    return {
        "task_id": task_id,
        "status": result.status,
        "result": result.result,
    }