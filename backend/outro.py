"""Auto Outro: dari isian nama channel + akun sosmed sampai video outro.

Dua langkah:

1. AI diminta menulis prompt untuk Google Flow, dengan kata-kata perintah
   yang sudah ditetapkan (PERMINTAAN di bawah).
2. Prompt itu ditempel ke Google Flow (labs.google/fx/tools/flow), videonya
   dibuat, lalu berkasnya diunduh ke folder media.

Langkah pertama SENGAJA tidak lewat peramban. Rencana awalnya membuka
chatgpt.com seperti yang dikerjakan tangan, tetapi diuji langsung dan tidak
bisa: Cloudflare menahan pemuatan halamannya sampai habis waktu, bahkan
dengan Chrome asli - permintaan polos pun dibalas 403. Yang dipakai sekarang
DeepSeek lewat API resminya, yang kuncinya memang sudah terpasang di
aplikasi ini untuk bot komentar. Hasilnya sama - teks perintahnya persis
sama - tetapi selesai dalam hitungan detik, tanpa login dan tanpa jendela.

Langkah kedua tetap di peramban: Google Flow tidak punya API publik. Ia
butuh akun Google yang sudah masuk, dan di sinilah masalahnya - Google
MENOLAK login dari peramban yang dikendalikan otomasi ("browser ini mungkin
tidak aman"), jadi jendela login bawaan alat ini tidak bisa dipakai. Diuji
juga menyalin berkas cookie Chrome ke profil lain: gagal, satu pun cookie
Google tidak terbaca, sebab macOS mengunci isinya di Keychain.

Yang dipakai: menyambung ke Chrome MILIK PENGGUNA SENDIRI lewat port debug
(CDP). Tidak ada yang login lewat otomasi - sesinya memang sudah ada di
peramban itu, dan bot hanya menumpang memakainya. Chrome perlu dijalankan
sekali dengan --remote-debugging-port; outro_api menyediakan tombolnya.

HANYA untuk backend lokal (mode via PC). Server Railway 1 GB tidak sanggup
memuat aplikasi WebGL seberat Flow, dan login Google dari alamat pusat data
hampir pasti ditolak. Penjaganya ada di outro_api.

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

# Kata-kata perintah untuk AI, persis seperti yang ditetapkan. Yang diganti
# hanya bagian dalam kurung siku.
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

# Flow pindah dari labs.google/fx/tools/flow ke alamat ini; yang lama masih
# dialihkan (308), tapi satu pengalihan di tiap pekerjaan tidak ada gunanya.
URL_FLOW = "https://flow.google.com/"

# Penulis prompt. Bawaannya DeepSeek, sebab kuncinya sudah ada di aplikasi
# ini. Cukup ganti tiga setelan ini kalau mau pindah ke penyedia lain yang
# bicara dengan gaya OpenAI (Kimi, Gemini lewat lapisan sesuai, dan
# sejenisnya) - kodenya tidak perlu diubah.
AI_KUNCI = os.getenv("OUTRO_AI_KEY", "") or os.getenv("DEEPSEEK_API_KEY", "")
AI_ALAMAT = (os.getenv("OUTRO_AI_URL", "") or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")).rstrip("/")
# Model termurah yang ditawarkan DeepSeek: 0,15-0,30 USD per sejuta token
# masuk dan 0,60-1,20 per sejuta token keluar, sekitar seperempat harga
# deepseek-v4-pro. DEEPSEEK_MODEL milik bot komentar SENGAJA tidak dipakai
# di sini: isinya "deepseek-chat", nama lama yang sudah tidak ada di daftar
# resmi dan suatu saat bisa dihentikan tanpa kabar.
AI_MODEL = os.getenv("OUTRO_AI_MODEL", "") or "deepseek-flash"

# Alamat port debug Chrome pengguna. Kalau terbuka, ia yang dipakai - sesi
# Google-nya sudah ada di sana. Kalau tidak, dicoba profil tersimpan hasil
# jendela login (masih berguna untuk platform yang tidak menolak otomasi).
CDP_URL = os.getenv("OUTRO_CDP", "http://127.0.0.1:9222")

# Jendelanya SENGAJA terlihat, bukan headless. Ini berjalan di Mac developer,
# dan kedua situs jauh lebih jarang mencurigai peramban yang benar-benar
# tampil. Bisa dimatikan lewat OUTRO_HEADLESS=1 untuk percobaan.
HEADLESS = os.getenv("OUTRO_HEADLESS", "").lower() in ("1", "true", "yes")
# Berapa lama menunggu AI menulis prompt, dan Flow selesai membuat video.
TUNGGU_AI = float(os.getenv("OUTRO_TUNGGU_AI", "120"))
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

    Akun yang kosong ditulis "-" supaya kalimatnya tetap utuh dan AI tidak
    mengarang username untuk mengisi kekosongan.
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
            _catat(job_id, "Meminta AI menulis prompt untuk Google Flow ...",
                   status="running", langkah="ai", progress=5)
            prompt = minta_prompt_ai(job["permintaan"], lambda m: _catat(job_id, m), ev)
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

def cdp_hidup() -> bool:
    """True kalau ada Chrome yang mendengarkan di port debug."""
    import httpx

    try:
        r = httpx.get(f"{CDP_URL.rstrip('/')}/json/version", timeout=3)
        return r.status_code == 200
    except Exception:  # noqa: BLE001 - tidak ada = tidak hidup
        return False


def chrome_pengguna_jalan() -> bool:
    """True kalau Chrome biasa sedang jalan (tanpa port debug)."""
    return subprocess.run(
        ["pgrep", "-f", "Google Chrome.app/Contents/MacOS/Google Chrome"],
        capture_output=True,
    ).returncode == 0


def profil_chrome_pengguna() -> Path:
    """Folder profil Chrome pengguna di mesin ini."""
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Google/Chrome"
    return Path.home() / ".config/google-chrome"


def nyalakan_chrome_debug(tutup_dulu: bool = False) -> dict[str, Any]:
    """Jalankan Chrome pengguna dengan port debug terbuka.

    Chrome mengabaikan --remote-debugging-port kalau sudah ada instance yang
    memakai profil yang sama: ia hanya membuka tab baru di jendela lama. Jadi
    Chrome yang sedang jalan harus ditutup lebih dulu - dan itu menutup
    tab-tab yang sedang dibuka orang, sehingga tidak pernah dilakukan
    diam-diam. Pemanggil harus meminta secara tegas lewat tutup_dulu.
    """
    if cdp_hidup():
        return {"ok": True, "pesan": "Chrome dengan port debug sudah jalan."}
    if chrome_pengguna_jalan():
        if not tutup_dulu:
            return {
                "ok": False,
                "perlu_tutup": True,
                "pesan": (
                    "Chrome sedang terbuka tanpa port debug. Tutup Chrome dulu "
                    "(tab-tabnya akan dibuka kembali), lalu tekan tombol ini lagi."
                ),
            }
        subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to quit'],
                       capture_output=True)
        for _ in range(20):
            if not chrome_pengguna_jalan():
                break
            time.sleep(0.5)

    biner = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not Path(biner).is_file():
        return {"ok": False, "pesan": "Google Chrome tidak ditemukan di /Applications."}
    port = CDP_URL.rsplit(":", 1)[-1].strip("/")
    try:
        subprocess.Popen(
            [biner, f"--remote-debugging-port={port}",
             f"--user-data-dir={profil_chrome_pengguna()}",
             "--restore-last-session"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        return {"ok": False, "pesan": f"Gagal menjalankan Chrome: {error}"}
    for _ in range(30):
        time.sleep(0.5)
        if cdp_hidup():
            return {"ok": True, "pesan": "Chrome dibuka dengan port debug; sesi Google-mu siap dipakai."}
    return {"ok": False, "pesan": "Chrome dijalankan tetapi port debugnya tidak terbuka."}


def _profil(platform: str) -> Path:
    from autoupload.browser import profile_dir

    return profile_dir(platform, "default")


def punya_sesi(platform: str) -> bool:
    """Siap dipakai: lewat Chrome pengguna (CDP) atau profil tersimpan."""
    if platform == "flow" and cdp_hidup():
        return True
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
#  LANGKAH 1: PROMPT DARI AI
# ============================================================

# Kalimat perintah yang ikut terbawa ke jawaban. Sebagian model menyalin
# baris terakhir permintaan ("Buat tanpa penjelasan tambahan") ke dalam
# hasilnya; kalau ikut tertempel ke Flow, ia terbaca sebagai bagian dari
# adegan yang diminta.
_BARIS_PERINTAH = re.compile(
    r"^\s*(output only|only output|buat tanpa penjelasan|tanpa penjelasan tambahan|"
    r"no additional explanation|do not add any explanation)\b.*$",
    re.I,
)
_PAGAR_KODE = re.compile(r"^```[a-zA-Z]*\n?|\n?```$", re.S)


def _rapikan(teks: str) -> str:
    """Buang pagar blok kode dan kalimat perintah yang ikut tersalin."""
    teks = _PAGAR_KODE.sub("", teks.strip()).strip()
    baris = [b for b in teks.splitlines() if not _BARIS_PERINTAH.match(b)]
    return "\n".join(baris).strip()


def _panggil_ai(httpx: Any, permintaan: str, model: str) -> Any:
    """Satu permintaan ke penyedia AI, gaya OpenAI."""
    return httpx.post(
        f"{AI_ALAMAT}/chat/completions",
        timeout=TUNGGU_AI,
        headers={"Authorization": f"Bearer {AI_KUNCI.strip()}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {
                    "role": "system",
                    # Tanpa ini sebagian model membalas dengan basa-basi
                    # ("Tentu, ini promptnya:") yang ikut tertempel ke Flow.
                    "content": (
                        "Kamu penulis prompt untuk pembuat video AI. Balas HANYA "
                        "dengan teks prompt akhirnya, tanpa pembuka, tanpa penutup, "
                        "tanpa tanda kutip, dan tanpa blok kode."
                    ),
                },
                {"role": "user", "content": permintaan},
            ],
            "temperature": 0.7,
        },
    )


def _model_tersedia(httpx: Any) -> str:
    """Nama model pertama yang benar-benar ditawarkan penyedianya sekarang."""
    try:
        r = httpx.get(
            f"{AI_ALAMAT}/models",
            timeout=30,
            headers={"Authorization": f"Bearer {AI_KUNCI.strip()}"},
        )
        if r.status_code != 200:
            return ""
        daftar = [str(m.get("id") or "") for m in r.json().get("data", [])]
        return next((m for m in daftar if m), "")
    except Exception:  # noqa: BLE001 - ini jalan cadangan, bukan jalan utama
        return ""


def minta_prompt_ai(
    permintaan: str, log: Callable[[str], None], ev: threading.Event
) -> str:
    """Minta AI menulis prompt Google Flow. Kembalikan teksnya, sudah rapi.

    Dipanggil dengan gaya OpenAI (POST /chat/completions), jadi penyedia mana
    pun yang bicara dengan gaya itu bisa dipakai hanya dengan mengganti tiga
    setelan di atas - tidak ada yang khusus DeepSeek di sini selain bawaannya.
    """
    import httpx

    if not AI_KUNCI.strip():
        raise OutroError(
            "Kunci AI belum diisi. Isi DEEPSEEK_API_KEY (atau OUTRO_AI_KEY) di "
            "berkas .env backend."
        )
    if ev.is_set():
        raise Dibatalkan("Dihentikan sebelum meminta prompt.")

    log(f"Mengirim permintaan ke {AI_MODEL} ...")
    try:
        balasan = _panggil_ai(httpx, permintaan, AI_MODEL)
        if balasan.status_code in (400, 404) and "model" in balasan.text.lower():
            # Nama model yang dipakai sudah tidak dikenal penyedianya. Ini
            # bukan hal jauh: saat perbaikan ini dibuat, daftar resmi DeepSeek
            # sudah tidak memuat "deepseek-chat" walau namanya masih diterima.
            # Daripada gagal dengan pesan yang tidak bisa ditindaklanjuti,
            # diambil model pertama yang memang tersedia sekarang.
            pengganti = _model_tersedia(httpx)
            if pengganti and pengganti != AI_MODEL:
                log(f"Model {AI_MODEL} sudah tidak ada; memakai {pengganti}.")
                logger.warning("model AI %s ditolak, beralih ke %s", AI_MODEL, pengganti)
                balasan = _panggil_ai(httpx, permintaan, pengganti)
    except httpx.HTTPError as error:
        raise OutroError(f"Tidak bisa menghubungi AI: {type(error).__name__}") from error

    if balasan.status_code == 401:
        raise OutroError("Kunci AI ditolak (401). Periksa DEEPSEEK_API_KEY di .env backend.")
    if balasan.status_code == 402:
        raise OutroError("Saldo AI habis (402). Isi ulang atau pakai kunci lain.")
    if balasan.status_code != 200:
        raise OutroError(
            f"AI menolak permintaan ({balasan.status_code}): {balasan.text[:160]}"
        )
    try:
        isi = balasan.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as error:
        raise OutroError("Balasan AI tidak bisa dibaca.") from error

    hasil = _rapikan(str(isi or ""))
    if len(hasil) < 40:
        raise OutroError(f"Jawaban AI terlalu pendek untuk dipakai: {hasil[:80]!r}")
    return hasil


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
        # Peramban pengguna sendiri kalau port debugnya terbuka: di sanalah
        # sesi Google yang sah berada, dan tidak ada yang perlu login lewat
        # otomasi. Peramban itu MILIK ORANG, jadi tidak ditutup di akhir -
        # hanya tabnya yang dirapikan.
        lewat_cdp = cdp_hidup()
        peramban = None
        if lewat_cdp:
            log("Memakai Chrome-mu yang sudah masuk (port debug).")
            peramban = pw.chromium.connect_over_cdp(CDP_URL)
            konteks = peramban.contexts[0] if peramban.contexts else peramban.new_context()
            page = konteks.new_page()
        else:
            konteks = _buka_profil(pw, "flow")
            page = konteks.pages[0] if konteks.pages else konteks.new_page()
        konteks.set_default_timeout(20_000)
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
                if lewat_cdp:
                    # Tab kita saja yang ditutup; jendela orangnya dibiarkan.
                    page.close()
                    if peramban is not None:
                        peramban.close()  # memutus sambungan, bukan menutup Chrome
                else:
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
