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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import redis

import storage
from celery_app import celery_app
from video_edit import (
    MEDIA_DIR,
    Dibatalkan,
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
# Job milik tamu dibuang jauh lebih cepat. Janjinya kepada tamu adalah "tidak
# ada yang tersimpan setelah keluar", jadi menyimpan hasil rendernya seharian
# di server bertentangan dengan itu - sekaligus memakan disk yang tidak pernah
# ada yang mengambilnya lagi.
JOB_RETENTION_HOURS_TAMU = float(os.getenv("VIDEO_JOB_RETENTION_HOURS_TAMU", "1"))

KUNCI_JOB = "videojob:"
KUNCI_URUT = "videojob:urut"
# Penanda "tolong hentikan". Dipakai karena worker memakai kumpulan utas, dan
# utas tidak bisa ditembak dari luar seperti proses — jadi pembatalan harus
# ditanyakan, bukan dipaksakan. Umurnya dibatasi supaya penanda yang tidak
# sempat terpakai tidak tertinggal selamanya.
KUNCI_BATAL = "videojob:batal:"
BATAL_UMUR_DETIK = 3 * 3600
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

    # Job tamu: batas waktunya sendiri, jauh lebih pendek.
    batas_tamu = time.time() - JOB_RETENTION_HOURS_TAMU * 3600
    if batas_tamu > batas:
        for job_id in r.zrangebyscore(KUNCI_URUT, "-inf", batas_tamu):
            mentah = r.get(KUNCI_JOB + job_id)
            if not mentah:
                continue
            try:
                pemilik = str(json.loads(mentah).get("owner") or "")
            except json.JSONDecodeError:
                continue
            if not pemilik or pemilik.startswith("guest:"):
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
    # Sisa di Supabase yang folder lokalnya sudah lenyap lebih dulu.
    try:
        bersihkan_storage_yatim()
    except Exception as error:  # noqa: BLE001 - penyapuan tidak boleh menggagalkan render
        logger.warning("Penyapuan sisa Supabase gagal: %s", error)

    if dihapus:
        logger.info("%d job kedaluwarsa dibuang", dihapus)
    return dihapus


# Penyapuan sisa di Supabase mahal (banyak permintaan daftar), jadi dijalankan
# paling sering sekali per jam walau render terjadi berkali-kali.
KUNCI_SAPU = "videojob:sapu-yatim"
SAPU_JEDA_DETIK = int(os.getenv("VIDEO_SAPU_JEDA", "3600"))


def _umur_jam(item: dict[str, Any]) -> float:
    """Umur sebuah berkas Supabase dalam jam; -1 kalau tanggalnya tak terbaca."""
    teks = str(item.get("created_at") or item.get("updated_at") or "").strip()
    if not teks:
        return -1.0
    try:
        waktu = datetime.fromisoformat(teks.replace("Z", "+00:00"))
    except ValueError:
        return -1.0
    if waktu.tzinfo is None:
        waktu = waktu.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - waktu).total_seconds() / 3600.0


def bersihkan_storage_yatim(paksa: bool = False) -> dict[str, int]:
    """Buang berkas di Supabase yang job atau unggahannya sudah tidak ada.

    Pembersih yang lama berangkat dari DISK: ia menghapus folder job yang
    kedaluwarsa, dan sekalian salinannya di Supabase. Masalahnya folder itu
    sering hilang lebih dulu - worker memakai /tmp, yang disapu bersih setiap
    kali container dijalankan ulang - dan begitu folder lokalnya lenyap, tidak
    ada lagi yang tahu salinan Supabase-nya harus dibuang. Sisa itu menumpuk
    selamanya: saat perbaikan ini dibuat ada 85 berkas berumur lebih dari
    sehari, 1,58 GB, yang menurut aturan simpan 24 jam seharusnya sudah lama
    tidak ada.

    Jadi penyapuan ini berangkat dari SUPABASE, bukan dari disk. Yang dibuang
    hanya yang sudah lewat batas simpan DAN catatannya tidak ada lagi di
    Redis - job yang masih tercatat tidak disentuh, berapa pun umurnya.
    """
    hasil = {"job": 0, "unggahan": 0, "gagal": 0}
    if not storage.enabled():
        return hasil
    r = _r()
    if not paksa:
        try:
            if not r.set(KUNCI_SAPU, "1", ex=SAPU_JEDA_DETIK, nx=True):
                return hasil
        except redis.RedisError:
            return hasil

    def buang_isi(awalan: str) -> int:
        """Hapus semua berkas di bawah satu folder Supabase."""
        jumlah = 0
        for berkas in storage.list_prefix(awalan, limit=1000):
            nama = str(berkas.get("name") or "")
            if not nama:
                continue
            storage.delete(f"{awalan}/{nama}")
            jumlah += 1
        return jumlah

    # 1. Hasil render yang job-nya sudah tidak tercatat lagi.
    try:
        for folder in storage.list_prefix("jobs", limit=1000):
            job_id = str(folder.get("name") or "")
            if not job_id:
                continue
            try:
                if r.exists(KUNCI_JOB + _aman_id(job_id)):
                    continue
            except (redis.RedisError, VideoError):
                continue
            isi = storage.list_prefix(f"jobs/{job_id}", limit=1000)
            if not isi:
                continue
            # Job yang baru saja dibuat tetapi catatannya belum sempat
            # ditulis tidak boleh ikut terbuang, jadi umurnya ikut diperiksa.
            if max((_umur_jam(b) for b in isi), default=-1.0) < JOB_RETENTION_HOURS:
                continue
            for berkas in isi:
                nama = str(berkas.get("name") or "")
                if nama:
                    storage.delete(f"jobs/{job_id}/{nama}")
                    hasil["job"] += 1
    except storage.StorageError as error:
        logger.warning("Penyapuan hasil render di Supabase gagal: %s", error)
        hasil["gagal"] += 1

    # 2. Video sumber unggahan. Tidak pernah tercatat di Redis, jadi umurnya
    #    saja yang menentukan - batasnya sama dengan job yang memakainya.
    try:
        for folder in storage.list_prefix("uploads", limit=1000):
            id_unggahan = str(folder.get("name") or "")
            if not id_unggahan:
                continue
            isi = storage.list_prefix(f"uploads/{id_unggahan}", limit=1000)
            if not isi:
                continue
            if max((_umur_jam(b) for b in isi), default=-1.0) < JOB_RETENTION_HOURS:
                continue
            hasil["unggahan"] += buang_isi(f"uploads/{id_unggahan}")
    except storage.StorageError as error:
        logger.warning("Penyapuan unggahan di Supabase gagal: %s", error)
        hasil["gagal"] += 1

    if hasil["job"] or hasil["unggahan"]:
        logger.info(
            "Sisa di Supabase dibuang: %d berkas hasil render, %d berkas unggahan",
            hasil["job"], hasil["unggahan"],
        )
    return hasil


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
    r.delete(KUNCI_BATAL + job_id)
    r.zrem(KUNCI_URUT, job_id)


def minta_batal(job_id: str) -> None:
    """Tandai satu job supaya berhenti pada kesempatan berikutnya."""
    _r().set(KUNCI_BATAL + _aman_id(job_id), "1", ex=BATAL_UMUR_DETIK)


def diminta_batal(job_id: str) -> bool:
    try:
        return bool(_r().exists(KUNCI_BATAL + _aman_id(job_id)))
    except Exception:  # noqa: BLE001
        # Redis sedang bermasalah: lebih baik render diteruskan daripada
        # dihentikan karena salah paham.
        return False


def lupakan_batal(job_id: str) -> None:
    _r().delete(KUNCI_BATAL + _aman_id(job_id))


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
        # Alamat sumber disimpan terpisah: kolom "url" nanti ditimpa alamat
        # hasil render, sedangkan tombol "coba lagi" butuh yang asli.
        sumber_url=url,
        template_id=template_id,
        owner=str(owner or "").strip().lower(),
        texts=texts or {},
        output=None,
        error=None,
        log="Job masuk antrean.",
    )
    return job_id


# Jawaban ping worker diingat sebentar: halaman menanyakannya tiap kali
# dibuka, dan ping menunggu balasan lewat Redis.
_worker_cache: tuple[bool, float] = (False, 0.0)


def worker_hidup(paksa: bool = False) -> bool:
    """Apakah ada worker Celery yang siap mengerjakan render.

    Tanpa ini, job yang dikirim saat worker mati hanya diam di "Menunggu
    giliran" selamanya tanpa ada yang bisa menjelaskan kenapa.
    """
    global _worker_cache
    ada, kedaluwarsa = _worker_cache
    if not paksa and kedaluwarsa > time.time():
        return ada
    try:
        balasan = celery_app.control.ping(timeout=0.6)
        ada = bool(balasan)
    except Exception:  # noqa: BLE001 - Redis mati juga berarti tidak ada worker
        ada = False
    _worker_cache = (ada, time.time() + 10)
    return ada


def _durasi_video(berkas: Path) -> float:
    """Durasi hasil render dalam detik; 0 kalau tidak terbaca.

    Dipakai halaman untuk menyebut panjang videonya sebelum diunduh.
    """
    try:
        from video_edit import probe

        return round(float(probe(berkas).get("duration") or 0), 1)
    except Exception:  # noqa: BLE001 - keterangan tambahan, bukan hal kritis
        return 0.0


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
    teks_warna: str = "",
) -> dict[str, Any]:
    folder = job_path(job_id)
    catat = lambda teks: tulis_status(job_id, log=teks)  # noqa: E731
    try:
        # Job yang sudah diminta berhenti sebelum sempat mulai tidak perlu
        # dikerjakan sama sekali.
        if diminta_batal(job_id):
            lupakan_batal(job_id)
            tulis_status(job_id, status="dibatalkan", progress=0, log="Dihentikan sebelum mulai.")
            return {"job_id": job_id, "status": "dibatalkan"}
        template = load_template(template_id)
        # Penimpaan warna tulisan berita untuk render ini saja. Dipakai Auto
        # Edit & Upload, yang memakai banyak template sekaligus sehingga tidak
        # bisa menyimpan pilihan warna ke satu template tertentu.
        if teks_warna in ("black", "white"):
            template = {**template, "teks_warna": teks_warna}
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
            batal=lambda: diminta_batal(job_id),
        )
        # Sumber tidak dipakai lagi; hapus supaya disk hemat.
        sumber.unlink(missing_ok=True)

        # Simpan permanen ke Supabase supaya tidak ikut hilang saat container
        # di-deploy ulang, sekaligus memberi URL yang bisa di-seek di browser.
        url = ""
        jalur = ""
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
            durasi=_durasi_video(hasil),
            # Jalur di dalam bucket ikut disimpan. URL bisa kedaluwarsa
            # (signed URL berumur pendek), jalurnya tidak - dan server punya
            # kunci untuk mengambilnya kapan pun.
            storage_path=jalur if di_supabase else "",
            size=ukuran,
            log="Video siap diunggah.",
        )
        _bersihkan_berkas_kerja(folder, hasil, di_supabase)
        bersihkan_job_lama()
        return {"job_id": job_id, "status": "done", "output": str(hasil)}
    except Dibatalkan:
        logger.info("Job video %s dihentikan pengguna", job_id)
        lupakan_batal(job_id)
        tulis_status(job_id, status="dibatalkan", log="Pembuatan video dihentikan.")
        _bersihkan_setelah_gagal(job_id, folder)
        return {"job_id": job_id, "status": "dibatalkan"}
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
