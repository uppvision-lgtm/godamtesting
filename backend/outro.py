"""Auto Outro: dari isian nama channel + akun sosmed sampai video outro.

Alurnya persis seperti yang dikerjakan orang secara manual, hanya dijalankan
bot di peramban:

1. ChatGPT (chatgpt.com) diminta menulis prompt untuk Google Flow, dengan
   kata-kata perintah yang sudah ditetapkan (PERMINTAAN di bawah).
2. Prompt itu ditempel ke Google Flow (labs.google/fx/tools/flow), videonya
   dibuat, lalu berkasnya diunduh ke folder media.

Keduanya butuh akun yang sudah masuk, dan keduanya menolak login otomatis -
jadi loginnya dilakukan sekali oleh developer di jendela peramban (lihat
autoupload.login), profilnya tersimpan, dan bot memakai profil itu.

HANYA untuk backend lokal (mode via PC). Server Railway 1 GB tidak sanggup
menjalankan dua aplikasi web seberat ini, dan login Google dari alamat pusat
data hampir pasti ditolak. Penjaganya ada di outro_api.

Pekerjaan berjalan di utas biasa (bukan Celery): ini alat developer di mesin
sendiri, satu pekerjaan pada satu waktu, dan keadaannya cukup disimpan di
memori proses.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from video_edit import MEDIA_DIR

logger = logging.getLogger(__name__)

# Kata-kata perintah untuk ChatGPT, persis seperti yang ditetapkan. Yang
# diganti hanya bagian dalam kurung siku.
PERMINTAAN = """Buatkan prompt untuk Google Flow untuk menghasilkan video outro branding.
Nama media: {channel}
Spesifikasi:
Durasi 4 detik
Format portrait 9:16
Gaya premium, modern, profesional, dan minimalis
Fokus pada logo dan identitas media sosial
Gunakan dark background, light sweep, subtle particles, dan smooth motion graphics
Tampilkan logo {channel}
Tampilkan 6 akun media sosial dengan jelas
Jangan membuat manusia, peristiwa nyata, berita aktual, politik, atau footage dunia nyata
Jangan gunakan istilah "breaking news", "current events", atau "news footage"
Jadikan sebagai fictional brand identity animation / social media outro
Pastikan seluruh teks dan username ditulis persis tanpa perubahan
Hasil harus aman untuk Google Flow dan siap langsung digunakan
Akun media sosial:
Instagram: {instagram}
TikTok: {tiktok}
X: {x}
Threads: {threads}
Facebook: {facebook}
YouTube: {youtube}
Buat tanpa penjelasan tambahan."""

# Urutan akun di PERMINTAAN mengikuti teks perintahnya, bukan urutan isian.
KUNCI_AKUN = ("instagram", "tiktok", "x", "threads", "facebook", "youtube")

URL_CHATGPT = "https://chatgpt.com/"
URL_FLOW = "https://labs.google/fx/tools/flow"

# Jendelanya SENGAJA terlihat, bukan headless. Ini berjalan di Mac developer,
# dan kedua situs jauh lebih jarang mencurigai peramban yang benar-benar
# tampil. Bisa dimatikan lewat OUTRO_HEADLESS=1 untuk percobaan.
HEADLESS = os.getenv("OUTRO_HEADLESS", "").lower() in ("1", "true", "yes")
# Berapa lama menunggu ChatGPT selesai menulis, dan Flow selesai membuat video.
TUNGGU_CHATGPT = float(os.getenv("OUTRO_TUNGGU_CHATGPT", "180"))
TUNGGU_FLOW = float(os.getenv("OUTRO_TUNGGU_FLOW", "900"))

# Flag anti-deteksi yang dipakai autoupload, TANPA pembatas memorinya:
# yang di sana ditujukan untuk kontainer 1 GB, sedangkan Flow adalah aplikasi
# WebGL berat yang tidak akan hidup dengan tumpukan JS 256 MB.
ARGS_PERAMBAN = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-infobars",
    "--start-maximized",
    "--disable-extensions",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-translate",
    "--mute-audio",
]
VIEWPORT = {"width": 1280, "height": 860}


class OutroError(Exception):
    """Kegagalan yang pesannya layak dibaca orang."""


class Dibatalkan(Exception):
    """Pekerjaan dihentikan atas permintaan."""


def outro_dir() -> Path:
    return MEDIA_DIR / "outro"


def susun_permintaan(channel: str, akun: dict[str, str]) -> str:
    """Isi kata-kata perintah dengan nama channel dan akun-akunnya.

    Akun yang kosong ditulis "-" supaya kalimatnya tetap utuh dan ChatGPT
    tidak mengarang username untuk mengisi kekosongan.
    """
    nilai = {k: (str(akun.get(k) or "").strip() or "-") for k in KUNCI_AKUN}
    return PERMINTAAN.format(channel=channel.strip(), **nilai)


# ============================================================
#  DAFTAR PEKERJAAN (di memori proses)
# ============================================================

_jobs: dict[str, dict[str, Any]] = {}
_batal: dict[str, threading.Event] = {}
_kunci = threading.Lock()
# Satu pekerjaan pada satu waktu: dua peramban berebut profil yang sama
# hanya berakhir dengan profil terkunci.
_sedang_jalan = threading.Lock()


def _catat(job_id: str, pesan: str, **ubahan: Any) -> None:
    with _kunci:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.setdefault("logs", []).append(pesan)
        job["logs"] = job["logs"][-100:]
        job["message"] = pesan
        job.update(ubahan)
        job["updated"] = time.time()
    logger.info("outro %s: %s", job_id, pesan)


def baca_job(job_id: str) -> dict[str, Any] | None:
    with _kunci:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def daftar_job(batas: int = 20) -> list[dict[str, Any]]:
    with _kunci:
        semua = sorted(_jobs.values(), key=lambda j: j.get("created", 0), reverse=True)
        return [dict(j) for j in semua[:batas]]


def minta_batal(job_id: str) -> bool:
    ev = _batal.get(job_id)
    if ev is None:
        return False
    ev.set()
    _catat(job_id, "Diminta berhenti; menunggu langkah yang sedang berjalan selesai.")
    return True


def mulai_job(channel: str, akun: dict[str, str]) -> str:
    channel = " ".join(str(channel or "").split())
    if not channel:
        raise OutroError("Nama channel wajib diisi.")
    job_id = uuid.uuid4().hex[:12]
    permintaan = susun_permintaan(channel, akun)
    with _kunci:
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "langkah": "antre",
            "progress": 0,
            "channel": channel,
            "akun": {k: str(akun.get(k) or "").strip() for k in KUNCI_AKUN},
            "permintaan": permintaan,
            "prompt": "",
            "video": "",
            "message": "Menunggu giliran.",
            "logs": ["Pekerjaan masuk antrean."],
            "created": time.time(),
            "updated": time.time(),
        }
    _batal[job_id] = threading.Event()
    threading.Thread(target=_jalankan, args=(job_id,), name=f"outro-{job_id}", daemon=True).start()
    return job_id


def _jalankan(job_id: str) -> None:
    ev = _batal[job_id]
    folder = outro_dir() / job_id
    folder.mkdir(parents=True, exist_ok=True)
    with _sedang_jalan:
        try:
            if ev.is_set():
                raise Dibatalkan("Dihentikan sebelum mulai.")
            job = baca_job(job_id) or {}
            _catat(job_id, "Membuka ChatGPT untuk meminta prompt ...",
                   status="running", langkah="chatgpt", progress=5)
            prompt = minta_prompt_chatgpt(
                job["permintaan"], lambda m: _catat(job_id, m), ev, folder
            )
            (folder / "prompt.txt").write_text(prompt, encoding="utf-8")
            _catat(job_id, f"Prompt jadi ({len(prompt)} karakter).", prompt=prompt,
                   langkah="flow", progress=35)

            if ev.is_set():
                raise Dibatalkan("Dihentikan setelah prompt jadi.")
            video = buat_video_flow(prompt, lambda m: _catat(job_id, m), ev, folder)
            _catat(job_id, f"Video outro siap ({video.stat().st_size / 1_048_576:.1f} MB).",
                   status="done", langkah="selesai", progress=100, video=str(video))
        except Dibatalkan as error:
            _catat(job_id, str(error), status="dibatalkan", langkah="berhenti")
        except OutroError as error:
            _catat(job_id, f"Gagal: {error}", status="error", langkah="gagal")
        except Exception as error:  # noqa: BLE001 - apa pun sebabnya, halaman harus tahu
            logger.exception("outro %s gagal", job_id)
            _catat(job_id, f"Gagal: {type(error).__name__}: {error}", status="error", langkah="gagal")
        finally:
            _batal.pop(job_id, None)


# ============================================================
#  PERAMBAN
# ============================================================

def _profil(platform: str) -> Path:
    from autoupload.browser import profile_dir

    return profile_dir(platform, "default")


def punya_sesi(platform: str) -> bool:
    folder = _profil(platform)
    return folder.is_dir() and any(folder.iterdir())


def _buka_profil(pw: Any, platform: str) -> Any:
    """Konteks peramban dari profil login tersimpan. Chrome asli diutamakan."""
    from autoupload.browser import STEALTH_INIT_SCRIPT, _clear_stale_profile_locks

    folder = _profil(platform)
    if not (folder.is_dir() and any(folder.iterdir())):
        raise OutroError(
            f"Belum ada sesi login {platform}. Buka jendela login di halaman Auto Outro dulu."
        )
    _clear_stale_profile_locks(folder)
    luncur = dict(
        user_data_dir=str(folder), headless=HEADLESS, args=ARGS_PERAMBAN, viewport=VIEWPORT
    )
    try:
        konteks = pw.chromium.launch_persistent_context(channel="chrome", **luncur)
    except Exception:  # noqa: BLE001 - Chrome asli tidak terpasang
        konteks = pw.chromium.launch_persistent_context(**luncur)
    konteks.add_init_script(STEALTH_INIT_SCRIPT)
    return konteks


def _ke_depan() -> None:
    """macOS: bawa jendela Chrome ke depan supaya kelihatan apa yang terjadi."""
    if sys.platform != "darwin" or HEADLESS:
        return
    for app in ("Google Chrome", "Chromium"):
        if subprocess.run(["osascript", "-e", f'tell application "{app}" to activate'],
                          capture_output=True).returncode == 0:
            return


def _tunggu(page: Any, detik: float, ev: threading.Event) -> None:
    """Jeda yang tetap peka pada permintaan berhenti."""
    akhir = time.monotonic() + detik
    while time.monotonic() < akhir:
        if ev.is_set():
            raise Dibatalkan("Pekerjaan dihentikan.")
        page.wait_for_timeout(min(500, max(0, (akhir - time.monotonic()) * 1000)))


def _simpan_jejak(page: Any, folder: Path, nama: str) -> str:
    """Tangkapan layar + daftar tombol di halaman, untuk membaca kegagalan."""
    try:
        page.screenshot(path=str(folder / f"{nama}.png"), full_page=False)
        tombol = page.evaluate(
            """() => [...document.querySelectorAll('button,[role="button"],textarea,[contenteditable="true"],a')]
                .slice(0, 200)
                .map(e => [e.tagName, e.getAttribute('aria-label')||'', e.getAttribute('placeholder')||'',
                           (e.innerText||'').trim().slice(0,60), e.getAttribute('data-testid')||''].join(' | '))
                .join('\\n')"""
        )
        (folder / f"{nama}.txt").write_text(f"{page.url}\n\n{tombol}", encoding="utf-8")
        return f" [jejak: {nama}.png]"
    except Exception:  # noqa: BLE001 - jejak hanya pelengkap
        return ""


# ============================================================
#  LANGKAH 1: CHATGPT
# ============================================================

_PAGAR_KODE = re.compile(r"^```[a-zA-Z]*\n?|\n?```$", re.S)


def minta_prompt_chatgpt(
    permintaan: str, log: Callable[[str], None], ev: threading.Event, folder: Path
) -> str:
    """Kirim kata-kata perintah ke ChatGPT, kembalikan jawabannya utuh."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        konteks = _buka_profil(pw, "chatgpt")
        page = konteks.pages[0] if konteks.pages else konteks.new_page()
        try:
            page.goto(URL_CHATGPT, wait_until="domcontentloaded", timeout=60_000)
            _ke_depan()
            try:
                page.wait_for_selector("#prompt-textarea", timeout=45_000)
            except Exception as exc:  # noqa: BLE001
                jejak = _simpan_jejak(page, folder, "chatgpt-komposer")
                raise OutroError(
                    "Kotak tulis ChatGPT tidak muncul. Sesinya mungkin sudah keluar - "
                    f"login ulang lewat halaman Auto Outro.{jejak}"
                ) from exc
            if page.locator('[data-testid="login-button"]').count():
                raise OutroError("ChatGPT belum masuk. Login ulang lewat halaman Auto Outro.")

            log("Menulis permintaan di ChatGPT ...")
            kotak = page.locator("#prompt-textarea")
            kotak.click()
            kotak.fill(permintaan)
            page.wait_for_timeout(600)
            kirim = page.locator('[data-testid="send-button"]')
            if kirim.count():
                kirim.first.click()
            else:
                page.keyboard.press("Enter")

            log("Menunggu ChatGPT selesai menulis ...")
            # Selama menulis, tombol kirim berganti tombol berhenti. Ditunggu
            # sampai tombol itu hilang, atau isi jawabannya diam beberapa
            # detik (untuk jawaban yang selesai sebelum sempat terlihat).
            batas = time.monotonic() + TUNGGU_CHATGPT
            terakhir, diam_sejak = "", time.monotonic()
            while time.monotonic() < batas:
                _tunggu(page, 1.0, ev)
                jawaban = page.locator('[data-message-author-role="assistant"]')
                teks = jawaban.last.inner_text() if jawaban.count() else ""
                if teks != terakhir:
                    terakhir, diam_sejak = teks, time.monotonic()
                masih = page.locator('[data-testid="stop-button"]').count() > 0
                if teks and not masih and time.monotonic() - diam_sejak > 3.0:
                    break
            else:
                jejak = _simpan_jejak(page, folder, "chatgpt-lama")
                raise OutroError(f"ChatGPT tidak selesai menulis dalam {TUNGGU_CHATGPT:.0f} detik.{jejak}")

            pesan = page.locator('[data-message-author-role="assistant"]').last
            # Kalau jawabannya dibungkus blok kode, ambil isi bloknya saja -
            # innerText pesan ikut membawa label "Copy code" di kepalanya.
            kode = pesan.locator("pre code")
            hasil = kode.first.inner_text() if kode.count() else pesan.inner_text()
            hasil = _PAGAR_KODE.sub("", hasil.strip()).strip()
            if len(hasil) < 40:
                jejak = _simpan_jejak(page, folder, "chatgpt-pendek")
                raise OutroError(f"Jawaban ChatGPT terlalu pendek untuk dipakai: {hasil[:80]!r}.{jejak}")
            return hasil
        finally:
            try:
                konteks.close()
            except Exception:  # noqa: BLE001
                pass


# ============================================================
#  LANGKAH 2: GOOGLE FLOW
# ============================================================

# Label tombol Flow dalam dua bahasa; antarmukanya ikut bahasa akun Google.
_PROYEK_BARU = ("New project", "Proyek baru", "Create new project", "Buat proyek baru")
_UNDUH = ("Download", "Unduh")
_POTRET = ("9:16", "Portrait", "Potret", "Vertical", "Vertikal")


def _klik_teks(page: Any, kandidat: tuple[str, ...], peran: str = "button", timeout: int = 8_000) -> bool:
    for label in kandidat:
        loc = page.get_by_role(peran, name=re.compile(re.escape(label), re.I))
        if loc.count():
            try:
                loc.first.click(timeout=timeout)
                return True
            except Exception:  # noqa: BLE001 - coba label berikutnya
                continue
        loc = page.get_by_text(label, exact=False)
        if loc.count():
            try:
                loc.first.click(timeout=timeout)
                return True
            except Exception:  # noqa: BLE001
                continue
    return False


def buat_video_flow(
    prompt: str, log: Callable[[str], None], ev: threading.Event, folder: Path
) -> Path:
    """Tempel prompt ke Google Flow, tunggu videonya jadi, unduh ke folder."""
    from playwright.sync_api import sync_playwright

    tujuan = folder / "outro.mp4"
    with sync_playwright() as pw:
        konteks = _buka_profil(pw, "flow")
        konteks.set_default_timeout(20_000)
        page = konteks.pages[0] if konteks.pages else konteks.new_page()
        try:
            log("Membuka Google Flow ...")
            page.goto(URL_FLOW, wait_until="domcontentloaded", timeout=90_000)
            _ke_depan()
            _tunggu(page, 4.0, ev)
            if "accounts.google.com" in page.url or page.get_by_role(
                "button", name=re.compile(r"sign in|masuk", re.I)
            ).count():
                jejak = _simpan_jejak(page, folder, "flow-login")
                raise OutroError(
                    f"Google Flow meminta masuk. Login ulang lewat halaman Auto Outro.{jejak}"
                )

            # Proyek baru, supaya videonya tidak bercampur dengan proyek lain
            # dan hasil yang diambil pasti yang barusan dibuat.
            if not _klik_teks(page, _PROYEK_BARU):
                jejak = _simpan_jejak(page, folder, "flow-proyek")
                raise OutroError(f"Tombol proyek baru di Flow tidak ditemukan.{jejak}")
            _tunggu(page, 4.0, ev)

            # Kotak prompt: textarea atau kotak contenteditable di bilah bawah.
            kotak = page.locator("textarea, [contenteditable='true']").last
            try:
                kotak.wait_for(state="visible", timeout=30_000)
            except Exception as exc:  # noqa: BLE001
                jejak = _simpan_jejak(page, folder, "flow-kotak")
                raise OutroError(f"Kotak prompt Flow tidak muncul.{jejak}") from exc

            # Rasio potret 9:16 lewat menu pengaturan (ikon roda gigi/tune).
            _atur_potret(page, ev, log)

            log("Menempel prompt ke Flow ...")
            kotak.click()
            kotak.fill(prompt)
            _tunggu(page, 1.0, ev)
            jumlah_video_awal = page.locator("video").count()
            # Tombol kirimnya ikon panah tanpa teks; Enter di kotak juga jalan.
            if not _klik_teks(page, ("Generate", "Buat", "Create"), timeout=4_000):
                page.keyboard.press("Enter")

            log("Flow sedang membuat video (biasanya 1-3 menit) ...")
            batas = time.monotonic() + TUNGGU_FLOW
            while time.monotonic() < batas:
                _tunggu(page, 5.0, ev)
                if page.locator("video").count() > jumlah_video_awal:
                    break
                galat = page.get_by_text(re.compile(r"(failed|gagal|couldn.t|tidak dapat|error)", re.I))
                if galat.count():
                    jejak = _simpan_jejak(page, folder, "flow-gagal")
                    raise OutroError(f"Flow menolak: {galat.first.inner_text()[:160]}.{jejak}")
            else:
                jejak = _simpan_jejak(page, folder, "flow-lama")
                raise OutroError(f"Flow tidak selesai dalam {TUNGGU_FLOW / 60:.0f} menit.{jejak}")
            _tunggu(page, 3.0, ev)

            log("Video jadi, mengunduh ...")
            with page.expect_download(timeout=120_000) as unduhan:
                if not _unduh_terbaru(page, ev):
                    jejak = _simpan_jejak(page, folder, "flow-unduh")
                    raise OutroError(f"Tombol unduh di Flow tidak ditemukan.{jejak}")
            unduhan.value.save_as(str(tujuan))
            if not tujuan.is_file() or tujuan.stat().st_size < 10_000:
                raise OutroError("Berkas unduhan Flow kosong.")
            return tujuan
        finally:
            try:
                konteks.close()
            except Exception:  # noqa: BLE001
                pass


def _atur_potret(page: Any, ev: threading.Event, log: Callable[[str], None]) -> None:
    """Pilih rasio 9:16 kalau menunya ketemu; kalau tidak, promptnya sudah menyebut."""
    try:
        pengaturan = page.get_by_role(
            "button", name=re.compile(r"settings|pengaturan|tune|options|opsi", re.I)
        )
        if not pengaturan.count():
            return
        pengaturan.first.click()
        _tunggu(page, 1.0, ev)
        if _klik_teks(page, _POTRET, peran="option", timeout=3_000) or _klik_teks(
            page, _POTRET, timeout=3_000
        ):
            log("Rasio 9:16 dipilih.")
        page.keyboard.press("Escape")
    except Exception as error:  # noqa: BLE001 - rasio hanya penyempurna
        log(f"Rasio tidak sempat diatur ({type(error).__name__}); lanjut dengan bawaan.")


def _unduh_terbaru(page: Any, ev: threading.Event) -> bool:
    """Buka menu klip terbaru lalu tekan Unduh."""
    video = page.locator("video").last
    try:
        video.hover(timeout=5_000)
    except Exception:  # noqa: BLE001
        pass
    _tunggu(page, 0.8, ev)
    if _klik_teks(page, _UNDUH, timeout=4_000):
        _tunggu(page, 1.0, ev)
        # Sebagian versi menawarkan ukuran; ambil yang pertama tersedia.
        _klik_teks(page, ("1080p", "Original", "Asli", "720p"), peran="menuitem", timeout=3_000)
        return True
    # Menu titik tiga di kartu klip.
    menu = page.get_by_role("button", name=re.compile(r"more|lainnya|menu|options", re.I))
    if menu.count():
        try:
            menu.last.click(timeout=4_000)
            _tunggu(page, 0.8, ev)
            if _klik_teks(page, _UNDUH, peran="menuitem", timeout=4_000) or _klik_teks(
                page, _UNDUH, timeout=4_000
            ):
                _tunggu(page, 1.0, ev)
                _klik_teks(page, ("1080p", "Original", "Asli", "720p"), peran="menuitem", timeout=3_000)
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def bersihkan_lama(jam: float = 24.0) -> int:
    """Buang folder outro yang lebih tua dari batas."""
    akar = outro_dir()
    if not akar.is_dir():
        return 0
    batas = time.time() - jam * 3600
    dibuang = 0
    for folder in akar.iterdir():
        try:
            if folder.is_dir() and folder.stat().st_mtime < batas:
                shutil.rmtree(folder, ignore_errors=True)
                dibuang += 1
        except OSError:
            continue
    return dibuang
