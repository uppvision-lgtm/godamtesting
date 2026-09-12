"""Job render video: dijalankan Celery worker supaya API tidak ikut terbebani.

Status job ditulis ke ``status.json`` di dalam folder job, bukan disimpan di
memori proses, supaya API (uvicorn) dan worker (Celery) — dua proses berbeda —
membaca keadaan yang sama.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import storage
from celery_app import celery_app
from video_edit import (
    MEDIA_DIR,
    VideoError,
    download_source,
    jobs_dir,
    load_template,
    render,
)

logger = logging.getLogger(__name__)

# Job lama dibersihkan otomatis supaya disk server tidak penuh.
JOB_RETENTION_HOURS = float(os.getenv("VIDEO_JOB_RETENTION_HOURS", "24"))


def job_path(job_id: str) -> Path:
    aman = "".join(c for c in str(job_id) if c.isalnum() or c in "-_")[:64]
    if not aman:
        raise VideoError("ID job tidak sah")
    return jobs_dir() / aman


def baca_status(job_id: str) -> dict[str, Any]:
    berkas = job_path(job_id) / "status.json"
    try:
        return json.loads(berkas.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise VideoError(f"Job '{job_id}' tidak ditemukan") from error
    except json.JSONDecodeError:
        return {"job_id": job_id, "status": "error", "message": "Status job rusak"}


def tulis_status(job_id: str, **ubahan: Any) -> dict[str, Any]:
    folder = job_path(job_id)
    folder.mkdir(parents=True, exist_ok=True)
    berkas = folder / "status.json"
    try:
        data = json.loads(berkas.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {"job_id": job_id, "logs": [], "progress": 0, "created": time.time()}
    catatan = ubahan.pop("log", None)
    if catatan:
        data.setdefault("logs", []).append(catatan)
        data["logs"] = data["logs"][-200:]
        data["message"] = catatan
    data.update(ubahan)
    data["job_id"] = job_id
    data["updated"] = time.time()
    sementara = berkas.with_suffix(".tmp")
    sementara.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    sementara.replace(berkas)
    return data


def daftar_job(batas: int = 20) -> list[dict[str, Any]]:
    hasil = []
    for folder in jobs_dir().iterdir():
        if not folder.is_dir():
            continue
        try:
            hasil.append(baca_status(folder.name))
        except VideoError:
            continue
    hasil.sort(key=lambda d: d.get("created") or 0, reverse=True)
    return hasil[:batas]


def bersihkan_job_lama() -> int:
    """Hapus folder job yang lebih tua dari batas simpan."""
    batas = time.time() - JOB_RETENTION_HOURS * 3600
    dihapus = 0
    for folder in jobs_dir().iterdir():
        if not folder.is_dir():
            continue
        try:
            dibuat = (baca_status(folder.name).get("created") or 0)
        except VideoError:
            dibuat = folder.stat().st_mtime
        if dibuat < batas:
            shutil.rmtree(folder, ignore_errors=True)
            dihapus += 1
    return dihapus


def buat_job(url: str, template_id: str, texts: dict[str, str] | None = None) -> str:
    """Siapkan folder + status awal, lalu kembalikan ID job."""
    job_id = uuid.uuid4().hex[:12]
    tulis_status(
        job_id,
        status="queued",
        progress=0,
        url=url,
        template_id=template_id,
        texts=texts or {},
        output=None,
        error=None,
        log="Job masuk antrean.",
    )
    return job_id


@celery_app.task(bind=True, name="render_video")
def render_video(
    self: Any,
    job_id: str,
    url: str,
    template_id: str,
    texts: dict[str, str] | None = None,
    session_id: str = "",
) -> dict[str, Any]:
    folder = job_path(job_id)
    catat = lambda teks: tulis_status(job_id, log=teks)  # noqa: E731
    try:
        template = load_template(template_id)
        tulis_status(job_id, status="downloading", progress=0, task_id=self.request.id)
        catat(f"Template: {template.get('name') or template_id}")
        sumber = download_source(url, folder, log=catat, session_id=session_id or None)

        tulis_status(job_id, status="rendering", progress=0)
        hasil = render(
            template,
            sumber,
            folder,
            texts=texts or {},
            log=catat,
            progress=lambda p: tulis_status(job_id, progress=p),
        )
        # Sumber tidak dipakai lagi; hapus supaya disk hemat.
        sumber.unlink(missing_ok=True)

        # Simpan permanen ke Supabase supaya tidak ikut hilang saat container
        # di-deploy ulang, sekaligus memberi URL yang bisa di-seek di browser.
        url = ""
        if storage.enabled():
            try:
                jalur = f"jobs/{job_id}/{hasil.name}"
                storage.upload(jalur, hasil, "video/mp4")
                url = storage.url_for(jalur)
                catat("Video tersimpan di Supabase.")
            except storage.StorageError as error:
                catat(f"Video jadi, tapi gagal disimpan ke Supabase: {error}")

        tulis_status(
            job_id,
            status="done",
            progress=100,
            output=hasil.name,
            url=url,
            size=hasil.stat().st_size,
            log="Video siap diunggah.",
        )
        bersihkan_job_lama()
        return {"job_id": job_id, "status": "done", "output": str(hasil)}
    except VideoError as error:
        logger.warning("Job video %s gagal: %s", job_id, error)
        tulis_status(job_id, status="error", error=str(error), log=f"Gagal: {error}")
        return {"job_id": job_id, "status": "error", "error": str(error)}
    except Exception as error:  # noqa: BLE001
        logger.exception("Job video %s gagal tak terduga", job_id)
        pesan = f"{type(error).__name__}: {error}"
        tulis_status(job_id, status="error", error=pesan, log=f"Gagal: {pesan}")
        return {"job_id": job_id, "status": "error", "error": pesan}
