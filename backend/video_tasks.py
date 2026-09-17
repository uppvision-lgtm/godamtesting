"""Job render video: dijalankan Celery worker supaya API tidak ikut terbebani.

Status job disimpan di Redis, bukan di berkas dalam folder job. Dua alasan:

1. API dan worker bisa berjalan sebagai dua layanan terpisah yang tidak
   berbagi disk. Selama status ada di berkas, keduanya wajib satu container.
2. Berkas status ditulis ulang utuh setiap kenaikan satu persen — sampai
   seratus kali per video, ditambah pembacaan dari API tiap dua detik.

Berkas besar (video sumber dan hasil render) tetap di disk; yang pindah hanya
keadaan kecil yang sering berubah.
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

import redis

import storage
from celery_app import celery_app
from video_edit import (
    MEDIA_DIR,
    VideoError,
    bersihkan_cache_unduhan,
    bersihkan_unggahan_lama,
    download_source,
    hapus_hasil_job,
    jobs_dir,
    load_template,
    render,
)

logger = logging.getLogger(__name__)

# Job lama dibersihkan otomatis supaya disk server tidak penuh.
JOB_RETENTION_HOURS = float(os.getenv("VIDEO_JOB_RETENTION_HOURS", "24"))

KUNCI_JOB = "videojob:"
KUNCI_URUT = "videojob:urut"
_redis_klien: redis.Redis | None = None


def _r() -> redis.Redis:
    global _redis_klien
    if _redis_klien is None:
        _redis_klien = redis.Redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
            socket_timeout=5,
        )
    return _redis_klien


def _aman_id(job_id: str) -> str:
    aman = "".join(c for c in str(job_id) if c.isalnum() or c in "-_")[:64]
    if not aman:
        raise VideoError("ID job tidak sah")
    return aman


def job_path(job_id: str) -> Path:
    """Folder kerja job di disk. Hanya dipakai worker, bukan API."""
    return jobs_dir() / _aman_id(job_id)


def baca_status(job_id: str) -> dict[str, Any]:
    isi = _r().get(KUNCI_JOB + _aman_id(job_id))
    if isi is None:
        raise VideoError(f"Job '{job_id}' tidak ditemukan")
    try:
        return json.loads(isi)
    except json.JSONDecodeError:
        return {"job_id": job_id, "status": "error", "message": "Status job rusak"}


def tulis_status(job_id: str, **ubahan: Any) -> dict[str, Any]:
    job_id = _aman_id(job_id)
    kunci = KUNCI_JOB + job_id
    r = _r()
    isi = r.get(kunci)
    try:
        data = json.loads(isi) if isi else None
    except json.JSONDecodeError:
        data = None
    if data is None:
        data = {"job_id": job_id, "logs": [], "progress": 0, "created": time.time()}
    catatan = ubahan.pop("log", None)
    if catatan:
        data.setdefault("logs", []).append(catatan)
        data["logs"] = data["logs"][-200:]
        data["message"] = catatan
    data.update(ubahan)
    data["job_id"] = job_id
    data["updated"] = time.time()
    umur = int(JOB_RETENTION_HOURS * 3600)
    pipa = r.pipeline()
    pipa.set(kunci, json.dumps(data, ensure_ascii=False), ex=umur)
    pipa.zadd(KUNCI_URUT, {job_id: data.get("created") or time.time()})
    pipa.execute()
    return data


def daftar_job(batas: int = 20, owner: str | None = None) -> list[dict[str, Any]]:
    """Job terbaru. ``owner`` menyaring per akun ("" = job tamu); None = semua."""
    r = _r()
    # Diambil dari yang terbaru; saat menyaring per akun, diambil lebih banyak
    # dulu karena sebagian akan tersisih.
    ambil = batas if owner is None else min(batas * 10, 1000)
    ids = r.zrevrange(KUNCI_URUT, 0, max(ambil, 1) - 1)
    if not ids:
        return []
    isi = r.mget([KUNCI_JOB + i for i in ids])
    hilang = []
    hasil = []
    for job_id, mentah in zip(ids, isi):
        if mentah is None:
            hilang.append(job_id)  # sudah kedaluwarsa sendiri di Redis
            continue
        try:
            status = json.loads(mentah)
        except json.JSONDecodeError:
            continue
        if owner is not None and str(status.get("owner") or "") != owner:
            continue
        hasil.append(status)
        if len(hasil) >= batas:
            break
    if hilang:
        r.zrem(KUNCI_URUT, *hilang)
    return hasil


def bersihkan_job_lama() -> int:
    """Buang job yang lebih tua dari batas simpan, beserta berkasnya."""
    batas = time.time() - JOB_RETENTION_HOURS * 3600
    dihapus = 0
    # Video sumber unggahan ikut kedaluwarsa bersama job-nya.
    try:
        bersihkan_unggahan_lama(JOB_RETENTION_HOURS * 3600)
    except OSError as error:
        logger.warning("Gagal membersihkan unggahan lama: %s", error)
    # Unduhan sumber yang sudah lewat umur cache ikut dibuang.
    try:
        bersihkan_cache_unduhan()
    except OSError as error:
        logger.warning("Gagal membersihkan cache unduhan: %s", error)

    r = _r()
    lama = r.zrangebyscore(KUNCI_URUT, "-inf", batas)
    for job_id in lama:
        buang_job(job_id)
        dihapus += 1

    # Folder kerja yang statusnya sudah hangus di Redis tidak punya pemilik
    # lagi; tanpa ini folder-folder itu tertinggal selamanya di disk.
    if jobs_dir().is_dir():
        for folder in list(jobs_dir().iterdir()):
            if not folder.is_dir():
                continue
            try:
                if folder.stat().st_mtime >= batas:
                    continue
            except OSError:
                continue
            if r.exists(KUNCI_JOB + folder.name):
                continue
            shutil.rmtree(folder, ignore_errors=True)
            dihapus += 1
    if dihapus:
        logger.info("%d job kedaluwarsa dibuang", dihapus)
    return dihapus


def buang_job(job_id: str) -> None:
    """Hapus satu job seluruhnya: berkas, salinan Supabase, dan catatannya.

    Sejak status pindah ke Redis, membuang berkasnya saja tidak cukup: tanpa
    ini job yang sudah dihapus tetap muncul di daftar sampai catatannya
    kedaluwarsa sendiri berjam-jam kemudian.
    """
    job_id = _aman_id(job_id)
    hapus_hasil_job(job_id)
    shutil.rmtree(jobs_dir() / job_id, ignore_errors=True)
    r = _r()
    r.delete(KUNCI_JOB + job_id)
    r.zrem(KUNCI_URUT, job_id)


def _bersihkan_berkas_kerja(folder: Path, hasil: Path | None, di_supabase: bool) -> None:
    """Buang sisa proses render yang tidak dipakai lagi.

    Yang dibuang: catatan ffmpeg, salinan perintahnya, dan gambar teks
    sementara. Semuanya hanya berguna selama render berlangsung, tetapi selama
    ini ikut tersimpan sampai seluruh folder job dihapus 24 jam kemudian.

    Video hasilnya sendiri hanya dibuang kalau salinannya sudah ada di
    Supabase; kalau belum, berkas lokal itu satu-satunya yang tersisa.
    """
    for nama in ("ffmpeg.log", "ffmpeg-command.txt"):
        (folder / nama).unlink(missing_ok=True)
    # Gambar teks ditulis langsung ke folder job (lihat _bangun_perintah, yang
    # menerima folder job sebagai folder kerja), bukan ke subfolder.
    for png in folder.glob("text_*.png"):
        png.unlink(missing_ok=True)
    shutil.rmtree(folder / "kerja", ignore_errors=True)
    if hasil is not None and di_supabase:
        hasil.unlink(missing_ok=True)
    # Kalau tidak ada lagi yang tersisa, foldernya sendiri ikut dibuang.
    try:
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    except OSError:
        pass


def buat_job(
    url: str, template_id: str, texts: dict[str, str] | None = None, owner: str = ""
) -> str:
    """Siapkan folder + status awal, lalu kembalikan ID job.

    ``owner`` = username pemilik job; kosong untuk tamu. Riwayat akun dibaca
    dari sini, dan job orang lain tidak bisa dilihat lewat API.
    """
    job_id = uuid.uuid4().hex[:12]
    tulis_status(
        job_id,
        status="queued",
        progress=0,
        url=url,
        template_id=template_id,
        owner=str(owner or "").strip().lower(),
        texts=texts or {},
        output=None,
        error=None,
        log="Job masuk antrean.",
    )
    return job_id


def _bersihkan_setelah_gagal(job_id: str, folder: Path) -> None:
    """Pembersihan yang dulu hanya jalan kalau render berhasil.

    Akibatnya, kalau semua job gagal, tidak ada apa pun yang pernah dibuang:
    video sumber, catatan ffmpeg, dan folder job lama menumpuk terus. Catatan
    ffmpeg sengaja DIPERTAHANKAN di sini karena isinya alasan kegagalannya;
    yang dibuang hanya berkas besar dan sisa job lama.
    """
    try:
        for sisa in folder.glob("source.*"):
            sisa.unlink(missing_ok=True)
        shutil.rmtree(folder / "kerja", ignore_errors=True)
    except OSError as galat:
        logger.warning("Sisa job %s gagal dibersihkan: %s", job_id, galat)
    try:
        bersihkan_job_lama()
    except Exception:  # noqa: BLE001
        logger.exception("Pembersihan job lama gagal")


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
        di_supabase = False
        if storage.enabled():
            try:
                jalur = f"jobs/{job_id}/{hasil.name}"
                storage.upload(jalur, hasil, "video/mp4")
                url = storage.url_for(jalur)
                di_supabase = True
                catat("Video tersimpan di Supabase.")
            except storage.StorageError as error:
                catat(f"Video jadi, tapi gagal disimpan ke Supabase: {error}")

        ukuran = hasil.stat().st_size
        tulis_status(
            job_id,
            status="done",
            progress=100,
            output=hasil.name,
            url=url,
            size=ukuran,
            log="Video siap diunggah.",
        )
        _bersihkan_berkas_kerja(folder, hasil, di_supabase)
        bersihkan_job_lama()
        return {"job_id": job_id, "status": "done", "output": str(hasil)}
    except VideoError as error:
        logger.warning("Job video %s gagal: %s", job_id, error)
        tulis_status(job_id, status="error", error=str(error), log=f"Gagal: {error}")
        _bersihkan_setelah_gagal(job_id, folder)
        return {"job_id": job_id, "status": "error", "error": str(error)}
    except Exception as error:  # noqa: BLE001
        logger.exception("Job video %s gagal tak terduga", job_id)
        pesan = f"{type(error).__name__}: {error}"
        tulis_status(job_id, status="error", error=pesan, log=f"Gagal: {pesan}")
        _bersihkan_setelah_gagal(job_id, folder)
        return {"job_id": job_id, "status": "error", "error": pesan}
