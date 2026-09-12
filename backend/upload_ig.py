"""Unggah video ke Instagram lewat otomasi browser (cookie sessionid).

Instagram tidak menyediakan API unggah untuk akun biasa, jadi langkahnya
ditiru seperti manusia: tombol Buat -> pilih berkas -> Berikutnya -> tulis
caption -> Bagikan.

Tampilan dan bahasa Instagram sering berubah, karena itu setiap langkah
mencoba beberapa kandidat penanda (Inggris dan Indonesia) dan mencatat apa
yang sedang dikerjakan supaya mudah ditelusuri kalau suatu saat berubah lagi.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Callable, Sequence

from playwright.async_api import Page

INSTAGRAM_URL = "https://www.instagram.com/"
LANGKAH_TIMEOUT_MS = int(os.getenv("UPLOAD_STEP_TIMEOUT_MS", "20000"))


class UploadError(RuntimeError):
    """Kegagalan yang layak ditampilkan apa adanya ke pengguna."""


TOMBOL_BUAT = (
    'svg[aria-label="New post"]',
    'svg[aria-label="Postingan baru"]',
    'svg[aria-label="New Post"]',
    'a[href="#"]:has(svg[aria-label="New post"])',
    '[aria-label="New post"]',
    '[aria-label="Postingan baru"]',
)

MENU_POST = ("Post", "Postingan", "Posting")
TOMBOL_PILIH_BERKAS = (
    "Select from computer",
    "Pilih dari komputer",
    "Select From Computer",
)
TOMBOL_LANJUT = ("Next", "Berikutnya", "Lanjut")
TOMBOL_BAGIKAN = ("Share", "Bagikan")
TOMBOL_OK = ("OK", "Oke", "Ok")
CAPTION_SELECTOR = (
    'textarea[aria-label="Write a caption..."]',
    'textarea[aria-label="Tulis keterangan..."]',
    'div[aria-label="Write a caption..."][contenteditable="true"]',
    'div[aria-label="Tulis keterangan..."][contenteditable="true"]',
    'div[role="textbox"][contenteditable="true"]',
)
PENANDA_SUKSES = (
    "your reel has been shared",
    "your post has been shared",
    "reel anda telah dibagikan",
    "postingan anda telah dibagikan",
    "post shared",
    "reel dibagikan",
)


async def _klik_teks(page: Page, kandidat: Sequence[str], timeout: int = 4000) -> bool:
    """Klik tombol/elemen pertama yang teksnya cocok salah satu kandidat."""
    for teks in kandidat:
        for pencari in (
            page.get_by_role("button", name=teks, exact=True),
            page.locator(f'div[role="button"]:has-text("{teks}")'),
            page.locator(f'button:has-text("{teks}")'),
            page.get_by_text(teks, exact=True),
        ):
            try:
                elemen = pencari.first
                if await elemen.is_visible(timeout=timeout // len(kandidat) or 500):
                    await elemen.click()
                    return True
            except Exception:
                continue
    return False


async def _klik_selector(page: Page, kandidat: Sequence[str], timeout: int = 4000) -> bool:
    for selector in kandidat:
        try:
            elemen = page.locator(selector).first
            if await elemen.is_visible(timeout=timeout // max(1, len(kandidat))):
                await elemen.click()
                return True
        except Exception:
            continue
    return False


async def _isi_caption(page: Page, caption: str) -> bool:
    for selector in CAPTION_SELECTOR:
        try:
            kotak = page.locator(selector).first
            if not await kotak.is_visible(timeout=3000):
                continue
            await kotak.click()
            # Diketik per potongan supaya Instagram sempat memproses masukan.
            for baris in caption.split("\n"):
                await page.keyboard.type(baris, delay=12)
                await page.keyboard.press("Shift+Enter")
            return True
        except Exception:
            continue
    return False


async def belum_login(page: Page) -> bool:
    """True kalau Instagram menampilkan formulir login.

    Instagram menyajikan formulir itu langsung di halaman depan tanpa
    mengalihkan ke /accounts/login, jadi memeriksa URL saja tidak cukup.
    """
    if "/accounts/login" in page.url.lower():
        return True
    # Kolom bertipe password di instagram.com hanya muncul saat belum login.
    # (Nama kolomnya sendiri berubah-ubah: pernah "password", kini "pass".)
    for selector in ('input[type="password"]', 'form[id*="login" i]'):
        try:
            if await page.locator(selector).first.count() and await page.locator(
                selector
            ).first.is_visible():
                return True
        except Exception:
            continue
    return False


async def _sudah_sukses(page: Page) -> bool:
    try:
        teks = (await page.locator("body").inner_text(timeout=3000)).lower()
    except Exception:
        return False
    return any(penanda in teks for penanda in PENANDA_SUKSES)


async def upload_video(
    page: Page,
    berkas: Path,
    caption: str,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Unggah satu video sebagai postingan/reel beserta caption-nya."""
    catat = log or (lambda _: None)
    berkas = Path(berkas)
    if not berkas.is_file():
        raise UploadError(f"Berkas video tidak ditemukan: {berkas}")

    catat("Membuka Instagram ...")
    await page.goto(INSTAGRAM_URL, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(3000)

    if await belum_login(page):
        raise UploadError(
            "Session ID tidak berlaku atau sudah kedaluwarsa — Instagram meminta login. "
            "Ambil ulang cookie sessionid dari browser yang sudah login."
        )

    # Tutup pop-up "Simpan info login" / "Aktifkan notifikasi" bila muncul.
    for teks in ("Not Now", "Nanti saja", "Tidak sekarang", "Not now"):
        try:
            tombol = page.get_by_role("button", name=teks).first
            if await tombol.is_visible(timeout=1200):
                await tombol.click()
        except Exception:
            continue

    catat("Menekan tombol Buat ...")
    if not await _klik_selector(page, TOMBOL_BUAT, timeout=8000):
        raise UploadError(
            "Tombol 'Buat/New post' tidak ditemukan. Tampilan Instagram mungkin berubah."
        )
    await page.wait_for_timeout(1500)
    # Pada sebagian tampilan, tombol Buat memunculkan menu berisi "Post".
    await _klik_teks(page, MENU_POST, timeout=2500)
    await page.wait_for_timeout(1500)

    catat("Memilih berkas video ...")
    try:
        async with page.expect_file_chooser(timeout=LANGKAH_TIMEOUT_MS) as penerima:
            if not await _klik_teks(page, TOMBOL_PILIH_BERKAS, timeout=6000):
                # Beberapa tampilan langsung memakai <input type=file> tersembunyi.
                await page.locator('input[type="file"]').first.set_input_files(str(berkas))
                raise asyncio.CancelledError
        chooser = await penerima.value
        await chooser.set_files(str(berkas))
    except asyncio.CancelledError:
        pass
    except Exception as error:
        raise UploadError(f"Gagal memilih berkas video: {error}") from error
    catat(f"Berkas dipilih: {berkas.name}")
    await page.wait_for_timeout(4000)

    # Video diunggah sebagai reel -> Instagram menampilkan pemberitahuan "OK".
    await _klik_teks(page, TOMBOL_OK, timeout=2500)
    await page.wait_for_timeout(1500)

    # Dua langkah "Berikutnya": pemotongan lalu penyuntingan.
    for nomor in (1, 2):
        if await _klik_teks(page, TOMBOL_LANJUT, timeout=8000):
            catat(f"Lanjut ke langkah {nomor + 1} ...")
            await page.wait_for_timeout(2500)
        else:
            catat(f"Tombol Berikutnya ke-{nomor} tidak ada, mungkin langkahnya dilewati.")

    if caption.strip():
        catat("Menulis caption ...")
        if not await _isi_caption(page, caption.strip()):
            catat("Kotak caption tidak ditemukan — lanjut tanpa caption.")
    await page.wait_for_timeout(1200)

    catat("Menekan Bagikan ...")
    if not await _klik_teks(page, TOMBOL_BAGIKAN, timeout=10000):
        raise UploadError("Tombol 'Bagikan/Share' tidak ditemukan")

    catat("Menunggu Instagram selesai memproses video ...")
    batas = int(os.getenv("UPLOAD_WAIT_SECONDS", "180"))
    for _ in range(batas):
        if await _sudah_sukses(page):
            catat("Instagram menyatakan video sudah dibagikan.")
            return {"status": "shared"}
        await page.wait_for_timeout(1000)
    raise UploadError(
        "Video sudah dikirim tetapi konfirmasi 'telah dibagikan' tidak muncul "
        f"dalam {batas} detik. Periksa akunmu sebelum mengunggah ulang."
    )


async def postingan_terbaru(page: Page, username: str) -> str:
    """URL postingan terbaru akun (pin dilewati) — dipakai untuk lanjut komentar."""
    from tasks import _SCAN_GRID_JS, close_popups, open_target_profile

    await open_target_profile(page, username)
    await close_popups(page)
    await page.wait_for_timeout(2000)
    for _ in range(5):
        item = await page.evaluate(_SCAN_GRID_JS)
        bukan_pin = [i["abs"] for i in item if not i.get("pinned")]
        if bukan_pin:
            return bukan_pin[0]
        await page.wait_for_timeout(1500)
    return ""
