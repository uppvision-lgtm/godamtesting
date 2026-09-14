"""Hook otomatis untuk Auto Edit Video: ubah caption jadi satu paragraf berita.

Bentuk yang ditiru adalah teks lower-third kanal berita:

    VIRAL! KONTROVERSI KARNAVAL DI PEKALONGAN DINILAI BERBAU SATANISME,
    1 PESERTA MENINGGAL TAK LAMA USAI ACARA

Dua bagian: ``kicker`` — satu kata pembuka yang menarik perhatian (VIRAL,
HEBOH, MIRIS, ...) yang dirender merah dan diberi tanda seru — lalu ``body``,
isi beritanya, mengalir di baris yang sama. Semua huruf kapital. Mesin render
sendiri yang memenggal jadi maksimal 4 baris rata kiri-kanan, jadi di sini
hanya panjang total yang dijaga.

Alurnya bertingkat supaya tidak pernah gagal total:

    1. DeepSeek menyusun kicker + body dari caption.
    2. Kalau DeepSeek mati / tanpa API key / balasannya tidak masuk akal,
       dipakai kalimat awal caption dengan kicker bawaan.

Modul ini SENGAJA tidak melakukan transkripsi audio: Whisper butuh CPU berat
dan server yang sama juga melayani auto-comment.tech yang harus hidup 24 jam.
"""

from __future__ import annotations

import json
import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

# Nama layer teks di template yang diisi paragraf ini.
NAMA_LAYER = "hook"

# Batas panjang isi (tanpa kicker). Pada font 38 px dengan lebar 660 px muat
# sekitar 28 karakter per baris; 4 baris ~ 110 karakter termasuk kicker.
# Renderer masih bisa menyusutkan font sedikit, jadi 100 aman.
MAX_KARAKTER_ISI = int(os.getenv("VIDEO_HOOK_MAX_CHARS", "100"))
MAX_KARAKTER_KICKER = 12
KICKER_BAWAAN = "VIRAL"

PROMPT = (
    "Anda adalah redaktur grafis berita televisi Indonesia. "
    "Baca caption video berikut, lalu tulis teks lower-third berita seperti "
    "kanal berita televisi: singkat, tegas, informatif, mudah dipindai. "
    "Jangan menyalin caption mentah-mentah dan jangan membuat opini atau "
    "fakta baru. "
    "Balas hanya JSON valid dengan dua field string: "
    "\"kicker\" berisi SATU kata pembuka yang menarik perhatian "
    "(contoh: VIRAL, HEBOH, MIRIS, TERUNGKAP, WASPADA, GEGER, HARU), tanpa "
    "tanda seru; dan \"body\" berisi isi beritanya dalam satu kalimat "
    f"padat maksimal {MAX_KARAKTER_ISI} karakter. Semua huruf kapital, "
    "tanpa label, tanpa markdown, tanpa nomor, tanpa tanda kutip."
    "\n\nCAPTION:\n{naskah}"
)

# Model sering tetap menempelkan label di awal walau sudah dilarang.
LABEL_AWALAN_RE = re.compile(
    r"^(HEADLINE|KETERANGAN|DAMPAK|PESAN|UTAMA|FAKTUAL|RINGKASAN|TIPS|KICKER|BODY|ISI)"
    r"\s*[:\-*]*\s*",
    re.IGNORECASE,
)
SAMPAH_AWAL_RE = re.compile(r"^[\s\-*\d.]+")        # bullet / penomoran
TANDA_AKHIR_RE = re.compile(r"[\s.!?]+$")


def _bersihkan(teks: str) -> str:
    teks = (teks or "").replace("**", "").replace('"', "").replace("“", "").replace("”", "")
    teks = SAMPAH_AWAL_RE.sub("", teks)
    teks = LABEL_AWALAN_RE.sub("", teks)
    teks = re.sub(r"\s+", " ", teks).strip()
    return TANDA_AKHIR_RE.sub("", teks).upper()


def _potong_di_kata(teks: str, batas: int) -> str:
    """Potong di batas karakter, mundur ke spasi terakhir supaya kata utuh."""
    if len(teks) <= batas:
        return teks
    potong = teks[:batas]
    if " " in potong:
        potong = potong[: potong.rfind(" ")]
    return potong.rstrip(" ,;:-")


def rapikan_kicker(teks: str) -> str:
    """Satu kata pembuka, kapital, tanpa tanda seru (ditambahkan saat digabung)."""
    bersih = _bersihkan(teks)
    kata = bersih.split()
    if not kata:
        return ""
    return re.sub(r"[^A-Z0-9]", "", kata[0])[:MAX_KARAKTER_KICKER]


def rapikan_isi(teks: str) -> str:
    """Isi paragraf: kapital, satu spasi, dipotong di batas kata."""
    return _potong_di_kata(_bersihkan(teks), MAX_KARAKTER_ISI)


def gabung(kicker: str, isi: str) -> str:
    """Rangkai jadi teks final: ``KICKER! ISI``."""
    kicker = kicker or KICKER_BAWAAN
    return f"{kicker}! {isi}".strip()


def dari_naskah(naskah: str) -> tuple[str, str]:
    """Cadangan tanpa AI: kicker bawaan + kalimat-kalimat awal caption."""
    kalimat = [k.strip() for k in re.split(r"[.!?\n]", str(naskah or "")) if k.strip()]
    isi = ""
    for k in kalimat:
        calon = f"{isi} {k}".strip()
        if len(calon) > MAX_KARAKTER_ISI and isi:
            break
        isi = calon
    isi = re.sub(r"[^A-Za-z0-9 ,]", " ", isi)
    return KICKER_BAWAAN, rapikan_isi(isi)


def _dari_jawaban(mentah: str) -> tuple[str, str] | None:
    """Ubah balasan JSON model jadi (kicker, isi). None kalau tidak layak."""
    if not mentah:
        return None
    data = None
    try:
        data = json.loads(mentah)
    except json.JSONDecodeError:
        cocok = re.search(r"\{.*\}", mentah, re.DOTALL)
        if cocok:
            try:
                data = json.loads(cocok.group(0))
            except json.JSONDecodeError:
                data = None
    if not isinstance(data, dict):
        return None
    kicker = rapikan_kicker(str(data.get("kicker", "")))
    isi = rapikan_isi(str(data.get("body", "")))
    if not isi:
        return None
    return (kicker or KICKER_BAWAAN), isi


async def buat_hook_deepseek(naskah: str, api_key: str | None = None) -> tuple[str, str] | None:
    """Minta DeepSeek menyusun kicker + isi. None kalau gagal."""
    naskah = re.sub(r"\s+", " ", str(naskah or "")).strip()
    if len(naskah) < 3:
        return None

    api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return None

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
                return None
            return _dari_jawaban(resp.json()["choices"][0]["message"]["content"])
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as error:
        logger.warning("Gagal membuat hook lewat DeepSeek: %s", error)
        return None


async def buat_hook(naskah: str) -> dict[str, object]:
    """Hasilkan hook beserta keterangan sumbernya.

    Selalu mengembalikan hasil selama ``naskah`` tidak kosong, karena ada
    cadangan tanpa AI. ``source`` berguna untuk ditampilkan di UI supaya
    pengguna tahu hasilnya dari AI atau dari ringkasan sederhana.
    """
    hasil = await buat_hook_deepseek(naskah)
    sumber = "deepseek"
    if hasil is None:
        hasil = dari_naskah(naskah)
        sumber = "cadangan"
    kicker, isi = hasil
    teks = gabung(kicker, isi) if isi else ""
    return {
        "kicker": kicker if isi else "",
        "body": isi,
        "hook": teks,
        "source": sumber,
        "texts": {NAMA_LAYER: teks} if teks else {},
    }
