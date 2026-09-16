"""API auto edit video: kelola template dan jalankan job render."""

from __future__ import annotations

import logging
import os
import subprocess
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, field_validator

import storage
import video_edit as ve
import video_hook as vh
import video_tasks as vt
from auth_api import pengguna_opsional, pengguna_wajib

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/video", tags=["video"])

# Render memakan CPU penuh, jadi jumlah job yang boleh menunggu dibatasi.
# Berapa render yang benar-benar berjalan bersamaan ditentukan CELERY_CONCURRENCY
# pada worker. Di sini cukup dibatasi panjang antreannya supaya tidak tanpa akhir.
MAX_QUEUED_JOBS = max(1, int(os.getenv("VIDEO_MAX_QUEUED_JOBS", "200")))
MAX_ASSET_MB = float(os.getenv("VIDEO_MAX_ASSET_MB", "200"))
# Sisi terpanjang gambar layer setelah dirapikan. Berkas desain kerap jauh
# lebih besar dari kanvas 720x1280 — satu berkas 3375x5706 memakan 77 MB
# begitu ffmpeg membukanya, dan beberapa layer sekaligus membuat proses render
# dimatikan kehabisan memori di server kecil. Batas ini masih di atas ukuran
# tampil terbesar (936 px), jadi ketajamannya tidak berkurang.
MAX_SISI_GAMBAR = int(os.getenv("VIDEO_MAX_IMAGE_SIDE", "1600"))
JENIS_GAMBAR = {".png", ".jpg", ".jpeg", ".webp"}
# Berlaku sama untuk video layer, intro, dan outro. Berkas 4K pada kanvas
# 720x1280 memaksa ffmpeg membongkar frame raksasa yang langsung dibuang
# setelah diperkecil; di container kecil itu membuat render dimatikan sistem.
MAX_SISI_VIDEO = int(os.getenv("VIDEO_MAX_VIDEO_SIDE", "1920"))
JENIS_VIDEO = {".mp4", ".mov", ".m4v", ".webm"}
JENIS_ASET = {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".m4v", ".webm"}
STATUS_AKTIF = ("queued", "downloading", "rendering")


class TemplateBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    width: int = Field(default=1080, ge=120, le=4096)
    height: int = Field(default=1920, ge=120, le=4096)
    fps: int = Field(default=30, ge=1, le=60)
    intro: str | None = None
    outro: str | None = None
    max_duration: float | None = Field(default=None, ge=1, le=600)
    overlays: list[dict[str, Any]] = []
    texts: list[dict[str, Any]] = []
    # Kotak teks hook dalam piksel kanvas: {x, y, w, h}. None = pakai posisi
    # bawaan layer teks.
    text_box: dict[str, int] | None = None
    badge_box: dict[str, int] | None = None
    # Keduanya menerima null: set yang dibuat sebelum field ini ada menyimpan
    # nilai kosong, dan membacanya lalu menyimpannya kembali harus tetap bisa.
    kategori: str | None = Field(default="", max_length=30)
    teks_warna: str | None = "white"

    @field_validator("kategori")
    @classmethod
    def _kategori_bersih(cls, v: str | None) -> str:
        return " ".join(str(v or "").split())

    @field_validator("teks_warna")
    @classmethod
    def _warna_sah(cls, v: str | None) -> str:
        return v if v in ("black", "white") else "white"


class DuplikatBody(BaseModel):
    """Nama untuk salinan set layer."""

    name: str = Field(min_length=1, max_length=100)


class BatchBody(BaseModel):
    """Satu link dirender dengan banyak set layer sekaligus."""

    url: str = Field(min_length=5, max_length=2000)
    template_ids: list[str] = Field(min_length=1, max_length=200)
    texts: dict[str, str] = {}
    session_id: str = ""


class JobBody(BaseModel):
    url: str = Field(min_length=5, max_length=2000)
    template_id: str = Field(min_length=1, max_length=80)
    texts: dict[str, str] = {}
    # Opsional: dipakai kalau link sumbernya dari Instagram (butuh cookie).
    session_id: str = ""


class PreviewBody(BaseModel):
    """Link video yang mau dilihat isinya sebelum diproses."""

    url: str = Field(min_length=5, max_length=2000)
    session_id: str = ""


class HookBody(BaseModel):
    """Naskah mentah dari pengguna yang mau dirapikan jadi 3 baris hook."""

    naskah: str = Field(min_length=3, max_length=5000)


def _rapikan_gambar(path: Path) -> int | None:
    """Pangkas pinggiran transparan lalu perkecil gambar layer bila kebesaran.

    Dua-duanya menghemat memori saat render. Pemangkasan juga membuat template
    tidak perlu memakai "crop" untuk membuang pinggiran kosong, sehingga
    ukuran berkas asli tidak lagi ikut menentukan hasil.

    Mengembalikan ukuran berkas baru, atau None kalau tidak ada yang diubah.
    """
    if path.suffix.lower() not in JENIS_GAMBAR:
        return None
    try:
        from PIL import Image

        with Image.open(path) as im:
            im = im.convert("RGBA")
            kotak = im.getbbox()
            berubah = False
            if kotak and kotak != (0, 0, im.width, im.height):
                im = im.crop(kotak)
                berubah = True
            sisi = max(im.width, im.height)
            if sisi > MAX_SISI_GAMBAR:
                skala = MAX_SISI_GAMBAR / sisi
                im = im.resize(
                    (max(1, round(im.width * skala)), max(1, round(im.height * skala))),
                    Image.LANCZOS,
                )
                berubah = True
            if not berubah:
                return None
            im.save(path, format="PNG")
    except Exception as error:  # berkas rusak atau format tak terduga
        logger.warning("Gambar %s tidak bisa dirapikan: %s", path.name, error)
        return None
    return path.stat().st_size


def _rapikan_video(path: Path) -> int | None:
    """Perkecil video layer/outro yang resolusinya jauh di atas kanvas.

    Hanya dijalankan sekali saat berkas diunggah, jadi setiap render sesudahnya
    ikut lebih ringan. Mengembalikan ukuran berkas baru, atau None kalau tidak
    ada yang diubah.
    """
    if path.suffix.lower() not in JENIS_VIDEO:
        return None
    try:
        info = ve.probe(path)
    except ve.VideoError as error:
        logger.warning("Video %s tidak bisa dibaca: %s", path.name, error)
        return None

    lebar, tinggi = int(info.get("width") or 0), int(info.get("height") or 0)
    if not lebar or not tinggi or max(lebar, tinggi) <= MAX_SISI_VIDEO:
        return None

    skala = MAX_SISI_VIDEO / max(lebar, tinggi)
    # Lebar dan tinggi dibulatkan ke angka genap; encoder yuv420p menolak
    # ukuran ganjil.
    baru_l = max(2, int(round(lebar * skala / 2)) * 2)
    baru_t = max(2, int(round(tinggi * skala / 2)) * 2)
    sementara = path.with_name(path.stem + "-kecil" + path.suffix)
    perintah = [
        ve.FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(path),
        "-vf", f"scale={baru_l}:{baru_t}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        # Audio disalin apa adanya kalau ada; kalau tidak, dilewati.
        "-c:a", "copy",
        str(sementara),
    ]
    try:
        hasil = subprocess.run(perintah, capture_output=True, text=True, timeout=600)
        if hasil.returncode != 0 or not sementara.is_file():
            # Sebagian berkas audionya tidak bisa disalin; ulangi dengan aac.
            perintah[perintah.index("copy")] = "aac"
            hasil = subprocess.run(perintah, capture_output=True, text=True, timeout=600)
        if hasil.returncode != 0 or not sementara.is_file():
            logger.warning(
                "Video %s gagal diperkecil: %s", path.name, (hasil.stderr or "")[:200]
            )
            sementara.unlink(missing_ok=True)
            return None
        sementara.replace(path)
    except (subprocess.TimeoutExpired, OSError) as error:
        logger.warning("Video %s gagal diperkecil: %s", path.name, error)
        sementara.unlink(missing_ok=True)
        return None

    logger.info("Video %s diperkecil %dx%d -> %dx%d", path.name, lebar, tinggi, baru_l, baru_t)
    return path.stat().st_size


def _tangani(error: ve.VideoError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(error))


# ============================================================
#  SIAPA BOLEH APA
# ============================================================
#
# Template punya pemilik (username) atau kosong = template bawaan. Tamu hanya
# melihat template bawaan dan boleh merender dengannya, tetapi tidak bisa
# menyimpan apa pun. Job pun dicatat pemiliknya, jadi riwayat satu akun tidak
# terlihat akun lain.


def _nama_akun(pengguna: dict[str, Any] | None) -> str | None:
    return str(pengguna["username"]) if pengguna else None


def _template_terlihat(template_id: str, pengguna: dict[str, Any] | None) -> dict[str, Any]:
    """Template yang boleh dibaca akun ini: miliknya sendiri atau template bawaan."""
    try:
        data = ve.load_template(template_id)
    except ve.VideoError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if not ve.boleh_lihat(data, _nama_akun(pengguna)):
        # 404, bukan 403: keberadaan template orang lain tidak perlu diumumkan.
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' tidak ditemukan")
    return data


def _periksa_ubah(data: dict[str, Any], pengguna: dict[str, Any] | None) -> None:
    nama = _nama_akun(pengguna)
    if not ve.boleh_lihat(data, nama):
        raise HTTPException(status_code=404, detail=f"Template '{data.get('id')}' tidak ditemukan")
    if not pengguna:
        raise HTTPException(status_code=401, detail="Masuk dulu untuk mengubah template.")
    if not ve.boleh_ubah(data, nama):
        raise HTTPException(
            status_code=403,
            detail="Template bawaan tidak bisa diubah. Duplikat dulu untuk menyuntingnya.",
        )


def _template_milik(template_id: str, pengguna: dict[str, Any] | None) -> dict[str, Any]:
    """Template yang boleh diubah akun ini. Tamu selalu ditolak."""
    try:
        data = ve.load_template(template_id)
    except ve.VideoError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    _periksa_ubah(data, pengguna)
    return data


def _job_terjangkau(job_id: str, pengguna: dict[str, Any] | None) -> dict[str, Any]:
    """Job milik akun ini; untuk tamu, job tamu (tanpa pemilik)."""
    try:
        data = vt.baca_status(job_id)
    except ve.VideoError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if str(data.get("owner") or "") != (_nama_akun(pengguna) or ""):
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' tidak ditemukan")
    return data


# ============================================================
#  PRATINJAU LINK
# ============================================================


@router.post("/preview")
def pratinjau_link(body: PreviewBody) -> dict[str, Any]:
    """Tampilkan isi link (judul, durasi, sampul) tanpa mengunduh videonya."""
    try:
        return ve.preview_source(body.url, body.session_id or None)
    except ve.VideoError as error:
        raise _tangani(error)


# ============================================================
#  HOOK OTOMATIS
# ============================================================


@router.post("/hook")
async def buat_hook(body: HookBody) -> dict[str, Any]:
    """Ubah naskah mentah jadi 3 baris lower-third siap tempel ke template.

    Tidak pernah melempar galat selama naskahnya terisi: kalau DeepSeek tidak
    tersedia, dipakai ringkasan sederhana dari naskah itu sendiri.
    """
    hasil = await vh.buat_hook(body.naskah)
    if not hasil["hook"]:
        raise HTTPException(
            status_code=400,
            detail="Naskah terlalu pendek untuk dijadikan hook.",
        )
    return hasil


# ============================================================
#  TEMPLATE
# ============================================================


@router.get("/templates")
def daftar_template(
    pengguna: dict[str, Any] | None = Depends(pengguna_opsional),
) -> dict[str, Any]:
    """Template milik akun yang masuk; untuk tamu, template bawaan saja."""
    return {"templates": ve.list_templates(owner=_nama_akun(pengguna))}


@router.post("/templates")
def buat_template(
    body: TemplateBody,
    template_id: str | None = None,
    pengguna: dict[str, Any] | None = Depends(pengguna_opsional),
) -> dict[str, Any]:
    """Buat atau perbarui template. Tamu tidak bisa menyimpan apa pun."""
    ada: dict[str, Any] | None = None
    if template_id:
        try:
            ada = ve.load_template(template_id)
        except ve.VideoError:
            ada = None
    if ada is not None:
        _periksa_ubah(ada, pengguna)
    elif not pengguna:
        raise HTTPException(status_code=401, detail="Masuk dulu untuk menyimpan template.")
    try:
        return ve.save_template(body.model_dump(), template_id, owner=_nama_akun(pengguna))
    except ve.VideoError as error:
        raise _tangani(error) from error


@router.get("/templates/{template_id}")
def ambil_template(
    template_id: str, pengguna: dict[str, Any] | None = Depends(pengguna_opsional)
) -> dict[str, Any]:
    data = _template_terlihat(template_id, pengguna)
    aset = sorted(
        p.name for p in (ve.template_path(template_id) / "assets").glob("*") if p.is_file()
    )
    # Kotak badge yang akan dipakai kalau pengguna belum menggambarnya, supaya
    # halaman bisa menampilkannya sebagai tebakan. can_edit memberi tahu
    # halaman apakah kendali penyuntingan perlu dikunci.
    return {
        **data,
        "assets": aset,
        "badge_box_default": ve.kotak_kategori_bawaan(data.get("text_box")),
        "can_edit": ve.boleh_ubah(data, _nama_akun(pengguna)),
    }


@router.get("/templates/{template_id}/preview.png")
def pratinjau_template(
    template_id: str, pengguna: dict[str, Any] | None = Depends(pengguna_opsional)
) -> Response:
    """Gambar komposit layer statis template (tanpa video), untuk halaman."""
    import io

    template = _template_terlihat(template_id, pengguna)
    try:
        kanvas = ve.komposit_statis(template)
    except ve.VideoError as error:
        raise _tangani(error)
    buf = io.BytesIO()
    kanvas.convert("RGB").save(buf, format="PNG", optimize=True)
    return Response(content=buf.getvalue(), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@router.post("/templates/{template_id}/detect-box")
def deteksi_kotak(
    template_id: str, pengguna: dict[str, Any] | None = Depends(pengguna_opsional)
) -> dict[str, Any]:
    """Tebak posisi kotak teks dari layer gambar template."""
    template = _template_terlihat(template_id, pengguna)
    try:
        kotak = ve.deteksi_kotak_teks(ve.komposit_statis(template))
    except ve.VideoError as error:
        raise _tangani(error)
    if kotak is None:
        raise HTTPException(status_code=404, detail="Tidak menemukan bidang polos yang cukup luas untuk kotak teks.")
    return {"text_box": kotak}


@router.post("/templates/{template_id}/duplicate")
def duplikat_template(
    template_id: str, body: DuplikatBody, pengguna: dict[str, Any] = Depends(pengguna_wajib)
) -> dict[str, Any]:
    """Buat set layer baru dengan isi yang sama, hanya namanya berbeda.

    Salinannya menjadi milik akun yang menyalin, juga saat sumbernya template
    bawaan. Itulah cara menyunting template bawaan.
    """
    _template_terlihat(template_id, pengguna)
    try:
        hasil = ve.duplicate_template(template_id, body.name, owner=pengguna["username"])
    except ve.VideoError as error:
        raise _tangani(error)
    aset = sorted(
        p.name for p in (ve.template_path(hasil["id"]) / "assets").glob("*") if p.is_file()
    )
    return {**hasil, "assets": aset, "badge_box_default": ve.kotak_kategori_bawaan(hasil.get("text_box"))}


@router.delete("/templates/{template_id}")
def hapus_template(
    template_id: str, pengguna: dict[str, Any] | None = Depends(pengguna_opsional)
) -> dict[str, bool]:
    _template_milik(template_id, pengguna)
    ve.delete_template(template_id)
    return {"ok": True}


@router.post("/templates/{template_id}/assets")
async def unggah_aset(
    template_id: str,
    file: UploadFile = File(...),
    pengguna: dict[str, Any] | None = Depends(pengguna_opsional),
) -> dict[str, Any]:
    """Terima satu berkas layer (PNG overlay, intro, atau outro)."""
    _template_milik(template_id, pengguna)

    nama = ve._aman(Path(file.filename or "aset").name)
    if Path(nama).suffix.lower() not in JENIS_ASET:
        raise HTTPException(
            status_code=415,
            detail=f"Jenis berkas tidak didukung. Pakai: {', '.join(sorted(JENIS_ASET))}",
        )
    folder = ve.template_path(template_id) / "assets"
    folder.mkdir(parents=True, exist_ok=True)
    tujuan = folder / nama
    batas = int(MAX_ASSET_MB * 1_048_576)
    ukuran = 0
    try:
        with tujuan.open("wb") as keluar:
            while potongan := await file.read(1_048_576):
                ukuran += len(potongan)
                if ukuran > batas:
                    raise HTTPException(
                        status_code=413, detail=f"Berkas melebihi {MAX_ASSET_MB:.0f} MB"
                    )
                keluar.write(potongan)
    except HTTPException:
        tujuan.unlink(missing_ok=True)
        raise

    ukuran = _rapikan_gambar(tujuan) or _rapikan_video(tujuan) or ukuran

    if storage.enabled():
        try:
            storage.upload(f"templates/{ve._aman(template_id)}/assets/{nama}", tujuan)
        except storage.StorageError as error:
            raise HTTPException(
                status_code=502, detail=f"Aset gagal disimpan ke Supabase: {error}"
            ) from error
    return {"file": f"assets/{nama}", "size": ukuran}


@router.delete("/templates/{template_id}/assets/{nama}")
def hapus_aset(
    template_id: str, nama: str, pengguna: dict[str, Any] | None = Depends(pengguna_opsional)
) -> dict[str, bool]:
    _template_milik(template_id, pengguna)
    berkas = ve.template_path(template_id) / "assets" / ve._aman(nama)
    berkas.unlink(missing_ok=True)
    return {"ok": True}


# ============================================================
#  JOB RENDER
# ============================================================


@router.post("/jobs")
def mulai_job(
    body: JobBody, pengguna: dict[str, Any] | None = Depends(pengguna_opsional)
) -> dict[str, Any]:
    _template_terlihat(body.template_id, pengguna)

    _pastikan_antrean_muat(1)
    job_id = vt.buat_job(
        body.url.strip(), body.template_id, body.texts, owner=_nama_akun(pengguna) or ""
    )
    vt.render_video.delay(
        job_id, body.url.strip(), body.template_id, body.texts, body.session_id.strip()
    )
    return {"job_id": job_id, "status": "queued"}


def _pastikan_antrean_muat(tambahan: int) -> None:
    semua = vt.daftar_job(500)
    antre = [j for j in semua if j.get("status") == "queued"]
    if len(antre) + tambahan > MAX_QUEUED_JOBS:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Antrean penuh: {len(antre)} job menunggu (maksimal {MAX_QUEUED_JOBS}). "
                "Tunggu sebagian selesai lalu coba lagi."
            ),
        )


@router.post("/sources")
async def unggah_sumber(file: UploadFile = File(...)) -> dict[str, Any]:
    """Terima video sumber dari komputer pengguna.

    Mengembalikan alamat "upload://<id>" yang dipakai persis seperti link:
    dikirim ke jobs/batch, lalu tiap job menyalinnya sebagai video sumber.
    """
    nama = ve._aman(Path(file.filename or "video").name)
    if Path(nama).suffix.lower() not in JENIS_VIDEO:
        raise HTTPException(
            status_code=415,
            detail=f"Jenis berkas tidak didukung. Pakai: {', '.join(sorted(JENIS_VIDEO))}",
        )
    id_unggahan = uuid.uuid4().hex[:12]
    folder = ve.uploads_dir() / id_unggahan
    folder.mkdir(parents=True, exist_ok=True)
    tujuan = folder / f"source{Path(nama).suffix.lower()}"
    batas = int(ve.MAX_SOURCE_UPLOAD_MB * 1_048_576)
    ukuran = 0
    try:
        with tujuan.open("wb") as keluar:
            while potongan := await file.read(1_048_576):
                ukuran += len(potongan)
                if ukuran > batas:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Video sumber melebihi {ve.MAX_SOURCE_UPLOAD_MB:.0f} MB.",
                    )
                keluar.write(potongan)
    except HTTPException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    try:
        info = ve.probe(tujuan)
    except ve.VideoError as error:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(status_code=415, detail=f"Berkas bukan video yang bisa dibaca: {error}")
    return {
        "url": f"{ve.AWALAN_UNGGAHAN}{id_unggahan}",
        "name": nama,
        "size": ukuran,
        "duration": info.get("duration"),
        "width": info.get("width"),
        "height": info.get("height"),
    }


@router.post("/jobs/batch")
def mulai_banyak_job(
    body: BatchBody, pengguna: dict[str, Any] | None = Depends(pengguna_opsional)
) -> dict[str, Any]:
    """Buat satu job per set layer untuk link yang sama.

    Worker mengerjakannya berurutan, dan video sumbernya diunduh sekali lalu
    dipakai ulang dari cache, jadi 100 set tidak berarti 100 unduhan.
    """
    ids: list[str] = []
    for tid in body.template_ids:
        tid = tid.strip()
        if tid and tid not in ids:
            ids.append(tid)
    if not ids:
        raise HTTPException(status_code=400, detail="Pilih minimal satu set layer.")
    for tid in ids:
        _template_terlihat(tid, pengguna)
    _pastikan_antrean_muat(len(ids))

    url = body.url.strip()
    pemilik = _nama_akun(pengguna) or ""
    jobs = []
    for tid in ids:
        job_id = vt.buat_job(url, tid, body.texts, owner=pemilik)
        vt.render_video.delay(job_id, url, tid, body.texts, body.session_id.strip())
        jobs.append({"job_id": job_id, "template_id": tid, "status": "queued"})
    return {"jobs": jobs}


@router.get("/jobs")
def daftar_job(
    limit: int = 20, pengguna: dict[str, Any] | None = Depends(pengguna_opsional)
) -> dict[str, Any]:
    """Riwayat render akun yang masuk. Tamu tidak punya riwayat."""
    if not pengguna:
        return {"jobs": []}
    return {"jobs": vt.daftar_job(max(1, min(100, limit)), owner=pengguna["username"])}


@router.get("/jobs/{job_id}")
def status_job(
    job_id: str, pengguna: dict[str, Any] | None = Depends(pengguna_opsional)
) -> dict[str, Any]:
    return _job_terjangkau(job_id, pengguna)


@router.get("/jobs/{job_id}/file")
def berkas_job(
    job_id: str, pengguna: dict[str, Any] | None = Depends(pengguna_opsional)
) -> FileResponse:
    status = _job_terjangkau(job_id, pengguna)
    if not status.get("output"):
        raise HTTPException(status_code=409, detail="Video belum selesai disusun")
    berkas = vt.job_path(job_id) / str(status["output"])
    if not berkas.is_file():
        # Cache lokal sudah dibersihkan; ambil lagi dari Supabase.
        if status.get("url"):
            return RedirectResponse(str(status["url"]))
        raise HTTPException(status_code=404, detail="Berkas hasil sudah tidak ada")
    return FileResponse(berkas, media_type="video/mp4", filename=f"{job_id}.mp4")


class BersihkanBody(BaseModel):
    """Daftar job yang hasil videonya sudah tidak dipakai."""

    job_ids: list[str] = Field(default_factory=list, max_length=500)


@router.delete("/jobs/{job_id}")
def hapus_job(
    job_id: str, pengguna: dict[str, Any] | None = Depends(pengguna_opsional)
) -> dict[str, bool]:
    _job_terjangkau(job_id, pengguna)
    ve.hapus_hasil_job(job_id)
    shutil.rmtree(vt.job_path(job_id), ignore_errors=True)
    return {"ok": True}


@router.post("/jobs/cleanup")
def bersihkan_job(
    body: BersihkanBody, pengguna: dict[str, Any] | None = Depends(pengguna_opsional)
) -> dict[str, int]:
    """Buang hasil render yang sudah tidak ditampilkan lagi.

    Dipakai halaman saat memulai render baru atau saat ditinggalkan. Memakai
    POST, bukan DELETE, supaya bisa dikirim lewat sendBeacon ketika tab
    ditutup — permintaan biasa sering keburu dibatalkan peramban.
    """
    dihapus = 0
    for job_id in body.job_ids[:500]:
        job_id = str(job_id).strip()
        if not job_id:
            continue
        try:
            _job_terjangkau(job_id, pengguna)
        except HTTPException:
            continue  # bukan miliknya, atau sudah tidak ada
        ve.hapus_hasil_job(job_id)
        shutil.rmtree(vt.job_path(job_id), ignore_errors=True)
        dihapus += 1
    return {"dihapus": dihapus}
