"""Hook otomatis untuk Auto Edit Video: ubah naskah mentah jadi 3 baris lower-third.

Gaya yang ditiru adalah lower-third kanal berita televisi: tiga baris pendek,
huruf kapital semua, maksimal 36 karakter per baris supaya tidak terpotong di
kanvas 9:16.

    Baris 1  headline utama yang menarik      (dirender mencolok, mis. merah)
    Baris 2  keterangan faktual isi video
    Baris 3  dampak, tips, atau pesan penting

Alurnya bertingkat supaya fitur ini tidak pernah gagal total:

    1. DeepSeek merapikan naskah jadi tiga baris.
    2. Kalau DeepSeek mati / tanpa API key / balasannya tidak masuk akal,
       dipakai ringkasan sederhana dari naskah itu sendiri (tiga kalimat
       pertama). Hasilnya tidak sepintar AI, tapi pengguna tetap dapat hook.

Catatan: modul ini SENGAJA tidak melakukan transkripsi audio. Whisper butuh CPU
berat dan server ini juga melayani auto-comment.tech yang harus hidup 24 jam.
Naskah diketik pengguna di halaman Auto Edit Video.
"""

from __future__ import annotations

import json
import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

# Batas karakter per baris. Lebih dari ini hampir pasti terpotong di layar 9:16.
MAX_KARAKTER = int(os.getenv("VIDEO_HOOK_MAX_CHARS", "36"))
JUMLAH_BARIS = 3

# Nama layer teks di template yang diisi otomatis, berurutan baris 1..3.
NAMA_BARIS = ("headline", "detail", "message")

PROMPT = (
    "Anda adalah redaktur grafis berita televisi Indonesia. "
    "Baca naskah berikut, lalu tulis lower-third berita seperti kanal berita "
    "televisi: singkat, tegas, informatif, dan mudah dipindai. "
    "Jangan menyalin ucapan mentah dan jangan membuat opini atau fakta baru. "
    "Jawab tepat 3 baris tanpa nomor, tanda kutip, bullet, atau label. "
    "Baris 1 adalah headline utama yang menarik (4-8 kata). "
    "Baris 2 adalah keterangan faktual tentang isi video. "
    "Baris 3 adalah dampak, tips, atau pesan penting. "
    f"Semua huruf kapital, maksimal {MAX_KARAKTER} karakter per baris. "
    "Jangan menulis label seperti HEADLINE, KETERANGAN, DAMPAK, atau PESAN "
    "dan jangan memakai markdown. "
    "Balas hanya JSON valid dengan tiga field string: headline, detail, message."
    "\n\nNASKAH:\n{naskah}"
)

# Model sering tetap menempelkan label di awal baris walau sudah dilarang.
LABEL_AWALAN_RE = re.compile(
    r"^(HEADLINE|KETERANGAN|DAMPAK|PESAN|UTAMA|FAKTUAL|RINGKASAN|TIPS)\s*[:\-*]*\s*",
    re.IGNORECASE,
)
# Sisa bullet / penomoran: "1. ", "- ", "* ", "2) " dan sebagainya.
SAMPAH_AWAL_RE = re.compile(r"^[\s\-*\d.]+")
TANDA_AKHIR_RE = re.compile(r"[\s.!?]+$")


def rapikan_baris(teks: str) -> str:
    """Bersihkan satu baris hook jadi siap tampil di layar.

    Membuang markdown, bullet, penomoran, dan label yang terbawa dari jawaban
    model; lalu kapitalkan dan potong tepat di ``MAX_KARAKTER``.
    """
    teks = (teks or "").replace("**", "")
    teks = SAMPAH_AWAL_RE.sub("", teks)
    teks = LABEL_AWALAN_RE.sub("", teks)
    teks = TANDA_AKHIR_RE.sub("", teks)
    # rstrip setelah dipotong: potongan keras di batas karakter sering menyisakan
    # spasi di ujung yang membuat teks terlihat tidak rata saat dirender.
    return teks.upper()[:MAX_KARAKTER].rstrip()


def dari_naskah(naskah: str) -> list[str]:
    """Cadangan tanpa AI: ambil tiga kalimat pertama naskah.

    Dipakai kalau DeepSeek tidak tersedia. Tanda baca dibuang supaya hasilnya
    tetap rapi sebagai teks layar, bukan kalimat utuh.
    """
    kalimat = [k.strip() for k in re.split(r"[.!?\n]", str(naskah or "")) if k.strip()]
    hasil: list[str] = []
    for k in kalimat[:JUMLAH_BARIS]:
        bersih = re.sub(r"[^A-Za-z0-9 ]", " ", k)
        bersih = re.sub(r"\s+", " ", bersih).strip().upper()
        if bersih:
            hasil.append(bersih[:MAX_KARAKTER].rstrip())
    return hasil


def _dari_jawaban(mentah: str) -> list[str]:
    """Ubah balasan JSON model jadi tiga baris bersih. [] kalau tidak layak."""
    if not mentah:
        return []
    data = None
    try:
        data = json.loads(mentah)
    except json.JSONDecodeError:
        # Model kadang membungkus JSON dengan basa-basi atau blok markdown.
        cocok = re.search(r"\{.*\}", mentah, re.DOTALL)
        if cocok:
            try:
                data = json.loads(cocok.group(0))
            except json.JSONDecodeError:
                data = None
    if not isinstance(data, dict):
        return []

    baris = [rapikan_baris(str(data.get(nama, ""))) for nama in NAMA_BARIS]
    # Semua baris wajib terisi; kalau ada yang kosong lebih baik pakai cadangan
    # daripada menampilkan baris bolong di video.
    return baris if all(baris) else []


async def buat_hook_deepseek(naskah: str, api_key: str | None = None) -> list[str]:
    """Minta DeepSeek merapikan naskah jadi tiga baris. [] kalau gagal."""
    naskah = re.sub(r"\s+", " ", str(naskah or "")).strip()
    if len(naskah) < 3:
        return []

    api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return []

    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Kamu redaktur grafis berita televisi Indonesia. "
                    "Balas hanya JSON valid, tanpa penjelasan tambahan."
                ),
            },
            {"role": "user", "content": PROMPT.format(naskah=naskah)},
        ],
        "temperature": 0.7,
        "max_tokens": 300,
        "response_format": {"type": "json_object"},
    }

    timeout = httpx.Timeout(60.0, connect=15.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code != 200:
                logger.warning("DeepSeek menolak permintaan hook: %s", resp.status_code)
                return []
            return _dari_jawaban(resp.json()["choices"][0]["message"]["content"])
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as error:
        logger.warning("Gagal membuat hook lewat DeepSeek: %s", error)
        return []


async def buat_hook(naskah: str) -> dict[str, object]:
    """Hasilkan hook tiga baris beserta keterangan sumbernya.

    Selalu mengembalikan hasil selama ``naskah`` tidak kosong, karena ada
    cadangan tanpa AI. ``sumber`` berguna untuk ditampilkan di UI supaya
    pengguna tahu hasilnya dari AI atau dari ringkasan sederhana.
    """
    baris = await buat_hook_deepseek(naskah)
    sumber = "deepseek"
    if not baris:
        baris = dari_naskah(naskah)
        sumber = "cadangan"

    return {
        "lines": baris,
        "source": sumber,
        "texts": {nama: isi for nama, isi in zip(NAMA_BARIS, baris)},
    }
