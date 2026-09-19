"""Bot komentar & like untuk Threads.

Kembaran ``tasks.py`` (Instagram) dengan alur yang sama persis: buka profil
target -> kumpulkan postingan terbaru (yang disematkan dilewati) -> per
postingan: like, hitung balasan milik akun ini, tambah balasan sampai target
memakai komentar DeepSeek (cadangan: bank lokal) sesuai nada yang dipilih.
Jeda antar aksi SAMA dengan Instagram: 3-6 detik antar balasan, DELAY_MIN..
DELAY_MAX detik antar postingan.

Yang berbeda hanya DOM-nya, dan tiga hal ini penting:

1. Membuka URL postingan secara langsung dilempar Threads ke beranda (feed
   "For you") dengan postingan itu di urutan pertama - bukan tampilan utas.
   Membawa header Referer profilnya membuat Threads membuka tampilan utas
   yang benar, berisi balasan dan kolom "Reply to ...".
2. Tombol aksi tidak punya aria-label. Yang ada hanya ``<svg><title>Like
   </title></svg>`` di dalam ``[role="button"]``; setelah disukai judulnya
   berubah jadi "Unlike". Semua pencarian tombol di sini lewat <title>.
3. Balasan tidak dikirim dengan Enter. Tombol Reply membuka dialog komposer
   dengan kolom contenteditable dan tombol "Post" yang harus diklik; Threads
   lalu menampilkan status "Posted".

Untuk menghindari pembatasan (throttle) Threads TANPA memperlambat jeda:
pengetikan per karakter dengan jeda acak seperti manusia, jumlah navigasi
ditekan (caption dibaca dari profil, bukan membuka tiap postingan dua kali),
dan setiap tanda pembatasan ("try again later", "we limit how often")
dideteksi lalu job dihentikan dengan pesan jelas, bukan terus menghantam.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from comment_ai import generate_comments as generate_ai_comments, normalize_tone
from tasks import (
    DELAY_MAX,
    DELAY_MIN,
    PLAYWRIGHT_NAVIGATION_TIMEOUT_MS,
    PLAYWRIGHT_TIMEOUT_MS,
    STATE_DIR,
    BotExecutionError,
    RetryableBotError,
    fallback_comments,
    generate_comments_from_bank,
)

logger = logging.getLogger(__name__)

THREADS_BASE_URL = "https://www.threads.com"
# Cookie dipasang di KEDUA domain: threads.net masih dipakai untuk pengalihan,
# dan kalau hanya salah satu, sesi tampak mati begitu domainnya berganti.
THREADS_COOKIE_DOMAINS = (".threads.net", ".threads.com")

# Judul <svg><title> tombol aksi, EN + ID.
_JUDUL_LIKE = ("Like", "Suka")
_JUDUL_UNLIKE = ("Unlike", "Batal suka", "Batalkan suka")
# Setelah akun ini pernah membalas, judul ikonnya berubah jadi "Replied" -
# sama polanya dengan Like -> Unlike. Tanpa varian ini, balasan kedua di
# postingan yang sama tidak pernah menemukan tombolnya.
_JUDUL_REPLY = ("Reply", "Replied", "Balas", "Dibalas", "Sudah dibalas", "Telah dibalas")
_TOMBOL_KIRIM = ("Post", "Kirim", "Posting")

# Tanda Threads sedang membatasi akun. Dicocokkan pada teks halaman dan pada
# daerah status/alert setelah setiap aksi.
_TANDA_THROTTLE = (
    "try again later",
    "coba lagi nanti",
    "we limit how often",
    "kami membatasi seberapa sering",
    "we restrict certain activity",
    "kami membatasi aktivitas tertentu",
    "action blocked",
    "aksi diblokir",
)
_TANDA_TIDAK_ADA = (
    "sorry, this page isn't available",
    "halaman ini tidak tersedia",
    "page not found",
    "halaman tidak ditemukan",
)

# Baris di blok postingan yang BUKAN isi postingan: nama akun, waktu, angka
# penghitung, dan kata-kata antarmuka. Dipakai saat memungut caption.
_BARIS_ANTARMUKA = re.compile(
    r"^(?:\d+[smhdw]|\d+ ?(?:m|h|d|w|min|mins|hour|hours|day|days|week|weeks|menit|jam|hari|minggu)(?: ago| yang lalu)?"
    r"|\d[\d.,]*[KMkm]?|[·/]|author|penulis|translate|terjemahkan|no replies yet|belum ada balasan"
    r"|view activity|lihat aktivitas|top|all|semua|teratas|pinned|disematkan|verified|terverifikasi"
    r"|reply to .*|balas ke .*|edited|diedit|reposted|memposting ulang|.* reposted|.* memposting ulang)$",
    re.IGNORECASE,
)


def _state_path(username: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", username)
    return STATE_DIR / f"threads_commented_state_{safe}.json"


def _load_commented_state(username: str) -> dict[str, int]:
    try:
        with _state_path(username).open(encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {u: int(n) for u, n in data.get("commented_posts", {}).items()}
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError) as error:
        logger.warning("Catatan komentar Threads rusak: %s", error)
    return {}


def _save_commented_state(username: str, commented_posts: dict[str, int]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _state_path(username)
    sementara = path.with_suffix(".tmp")
    with sementara.open("w", encoding="utf-8") as f:
        json.dump({"commented_posts": commented_posts}, f, indent=2)
    sementara.replace(path)


def _handle(target: str) -> str:
    """'@akun', 'akun', atau URL profil -> 'akun'."""
    t = target.strip()
    if t.startswith(("http://", "https://")):
        m = re.search(r"/@([^/?#]+)", t)
        return m.group(1) if m else t.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
    return t.lstrip("@")


def _target_url(target: str) -> str:
    return f"{THREADS_BASE_URL}/@{quote(_handle(target), safe='.')}"


def _bersihkan_caption(teks: str, handle: str) -> str:
    """Buang nama akun, waktu, penghitung, dan kata antarmuka dari blok postingan."""
    baris = []
    for b in str(teks or "").splitlines():
        b = b.strip()
        if not b or b.lower() == handle.lower() or _BARIS_ANTARMUKA.match(b):
            continue
        baris.append(b)
    return re.sub(r"\s+", " ", " ".join(baris)).strip()


# ============================================================
#  COOKIE, NAVIGASI, PEMERIKSAAN HALAMAN
# ============================================================


async def _add_session_cookie(page: Page, session_id: str) -> None:
    await page.context.add_cookies(
        [
            {"name": "sessionid", "value": session_id, "domain": domain, "path": "/"}
            for domain in THREADS_COOKIE_DOMAINS
        ]
    )


async def open_target_profile(page: Page, target: str) -> None:
    try:
        await page.goto(
            _target_url(target),
            wait_until="domcontentloaded",
            timeout=PLAYWRIGHT_NAVIGATION_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as error:
        raise RetryableBotError("Membuka profil Threads melewati batas waktu") from error
    await page.wait_for_timeout(2500)


async def _teks_status(page: Page) -> str:
    try:
        return await page.evaluate(
            """() => [...document.querySelectorAll('[role="alert"], [role="status"]')]
                    .map(e => (e.innerText || '').trim()).filter(Boolean).join(' | ')"""
        )
    except Exception:
        return ""


async def check_page_access(page: Page) -> None:
    url = page.url.lower()
    if "/login" in url or "accounts/login" in url:
        raise BotExecutionError("Sesi Threads sudah tidak valid (ter-logout)")
    try:
        kolom_sandi = page.locator('input[type="password"]').first
        if await kolom_sandi.count() and await kolom_sandi.is_visible():
            raise BotExecutionError("Sesi Threads sudah tidak valid (ter-logout)")
    except BotExecutionError:
        raise
    except Exception:
        pass
    try:
        badan = (await page.locator("body").inner_text(timeout=PLAYWRIGHT_TIMEOUT_MS)).lower()
    except Exception:
        return
    if any(t in badan for t in _TANDA_TIDAK_ADA):
        raise BotExecutionError("Akun Threads target tidak ditemukan")
    # "Log in" besar di tengah halaman = sesinya tidak diterima.
    if "log in with your instagram" in badan and "@" not in badan[:400]:
        raise BotExecutionError("Sesi Threads sudah tidak valid (ter-logout)")
    await _pastikan_tidak_dibatasi(page)


async def _pastikan_tidak_dibatasi(page: Page) -> None:
    """Berhenti begitu Threads memberi tanda pembatasan, jangan terus menghantam."""
    status = (await _teks_status(page)).lower()
    if any(t in status for t in _TANDA_THROTTLE):
        raise RetryableBotError(f"Threads membatasi aksi akun ini: {status[:120]}")


async def close_popups(page: Page) -> None:
    for label in ("Not now", "Not Now", "Nanti saja", "Lain kali", "OK", "Oke"):
        tombol = page.get_by_role("button", name=label, exact=True).first
        try:
            if await tombol.is_visible(timeout=400):
                await tombol.click()
                await page.wait_for_timeout(500)
        except Exception:
            continue


# ============================================================
#  DAFTAR POSTINGAN DI PROFIL
# ============================================================

_SCAN_PROFIL_JS = r"""(handle) => {
    // Tautan permalink postingan milik akun ini (repost milik orang lain punya
    // /@akunlain/post/ dan sengaja tidak ikut). Blok induknya dibaca untuk
    // caption dan penanda "Pinned".
    const items = [];
    const seen = new Set();
    const prefix = '/@' + handle + '/post/';
    for (const a of document.querySelectorAll('a[href*="/post/"]')) {
        const rel = (a.getAttribute('href') || '').split('?')[0].replace(/\/$/, '');
        if (!rel.toLowerCase().startsWith(prefix.toLowerCase())) continue;
        if (rel.endsWith('/media')) continue;
        if (seen.has(rel)) continue;
        seen.add(rel);
        // Naik sampai blok yang memuat tombol Like = satu kartu postingan.
        let n = a, wadah = null;
        for (let i = 0; i < 14 && n; i++) {
            n = n.parentElement;
            if (n && [...n.querySelectorAll('svg title')].some(t => /^(Like|Unlike|Suka|Batal suka)$/.test(t.textContent))) { wadah = n; break; }
        }
        const teks = wadah ? (wadah.innerText || '') : '';
        const pinned = /(^|\n)\s*(pinned|disematkan)\s*(\n|$)/i.test(teks)
            || (wadah ? [...wadah.querySelectorAll('svg title')].some(t => /pinned|disematkan/i.test(t.textContent)) : false);
        // Balasan si pemilik ke utasnya sendiri ikut tampil di profil sebagai
        // kartu lanjutan berlencana "2/2", "3/3", dst. Itu bukan postingan
        // baru; yang dihitung sebagai postingan hanya akar utasnya ("1/N"
        // atau tanpa lencana).
        const lencana = teks.match(/(?:^|\n)(\d+)\n\/\n(\d+)(?:\n|$)/);
        const lanjutan = !!(lencana && parseInt(lencana[1], 10) > 1);
        items.push({ rel: rel, pinned: pinned, lanjutan: lanjutan, teks: teks.slice(0, 3000) });
    }
    return items;
}"""


async def collect_posts(page: Page, target: str, limit: int, log: Any | None = None) -> list[dict[str, str]]:
    """Kumpulkan ``limit`` postingan TERBARU milik target dari profilnya.

    Caption diambil langsung dari blok di profil supaya tidak perlu membuka
    tiap postingan dua kali - makin sedikit navigasi, makin kecil peluang
    dibatasi. Postingan yang disematkan dilewati dan digantikan berikutnya.
    """
    handle = _handle(target)
    urut: list[str] = []
    pinned: set[str] = set()
    caption: dict[str, str] = {}
    for _ in range(25):
        for item in await page.evaluate(_SCAN_PROFIL_JS, handle):
            rel = item["rel"]
            if item.get("lanjutan"):
                continue
            if item["pinned"]:
                pinned.add(rel)
            if rel not in urut:
                urut.append(rel)
                caption[rel] = _bersihkan_caption(item.get("teks", ""), handle)
        bukan_pin = [u for u in urut if u not in pinned]
        if len(bukan_pin) >= limit:
            break
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
        await page.wait_for_timeout(1600)

    if log and pinned:
        log(
            f"Melewati {len(pinned)} postingan disematkan (pin): "
            f"{', '.join(u.rsplit('/', 1)[-1] for u in list(pinned)[:5])}"
        )
    bukan_pin = [u for u in urut if u not in pinned][:limit]
    return [{"url": THREADS_BASE_URL + rel, "caption": caption.get(rel, "")} for rel in bukan_pin]


async def open_post(page: Page, url: str, target: str) -> None:
    """Buka tampilan utas sebuah postingan.

    Tanpa Referer, Threads mengalihkan URL postingan ke beranda; dengan
    Referer profilnya, tampilan utas yang benar (berisi balasan) yang dibuka.
    """
    await page.goto(
        url,
        referer=_target_url(target),
        wait_until="domcontentloaded",
        timeout=PLAYWRIGHT_NAVIGATION_TIMEOUT_MS,
    )
    try:
        await page.wait_for_load_state("load", timeout=15000)
    except Exception:
        pass
    await page.wait_for_timeout(2500)
    if page.url.rstrip("/") in (THREADS_BASE_URL, THREADS_BASE_URL + "/"):
        # Masih dilempar ke beranda: tempuh jalan lain, klik dari profilnya.
        await open_target_profile(page, target)
        rel = url.replace(THREADS_BASE_URL, "")
        tautan = page.locator(f'a[href^="{rel}"]').first
        for _ in range(8):
            if await tautan.count():
                break
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(1200)
        await tautan.click(timeout=PLAYWRIGHT_TIMEOUT_MS)
        await page.wait_for_timeout(3000)


# ============================================================
#  KARTU: POSTINGAN UTAMA & BALASAN DI TAMPILAN UTAS
# ============================================================

# Satu "kartu" = wadah terdekat yang memuat tombol Like DAN tautan penulis.
# Kartu pertama di tampilan utas adalah postingan utama; sisanya balasan.
_KARTU_JS = r"""() => {
    const kartu = [];
    for (const t of document.querySelectorAll('svg title')) {
        if (!/^(Like|Unlike|Suka|Batal suka|Batalkan suka)$/.test(t.textContent)) continue;
        const tombol = t.closest('[role="button"], button');
        if (!tombol) continue;
        let n = tombol, wadah = null;
        for (let i = 0; i < 14 && n; i++) {
            n = n.parentElement;
            if (n && n.querySelector('a[href^="/@"]')) { wadah = n; break; }
        }
        if (!wadah) continue;
        const penulis = [...wadah.querySelectorAll('a[href^="/@"]')]
            .map(a => (a.innerText || '').trim()).filter(Boolean)[0] || '';
        kartu.push({ penulis: penulis, like: t.textContent, teks: (wadah.innerText || '').slice(0, 3000) });
    }
    return kartu;
}"""

# Tombol aksi (berdasarkan <title>) milik kartu ke-n. Dikembalikan sebagai
# elemen supaya bisa diklik Playwright.
_TOMBOL_KARTU_JS = r"""([judul, indeks]) => {
    const daftar = judul.split('|');
    let ke = -1;
    for (const t of document.querySelectorAll('svg title')) {
        if (!/^(Like|Unlike|Suka|Batal suka|Batalkan suka)$/.test(t.textContent)) continue;
        ke += 1;
        if (ke !== indeks) continue;
        const likeBtn = t.closest('[role="button"], button');
        // Tombol yang dicari ada di bar aksi yang sama dengan tombol Like ini.
        let bar = likeBtn ? likeBtn.parentElement : null;
        for (let i = 0; i < 4 && bar; i++) {
            const cocok = [...bar.querySelectorAll('svg title')].find(x => daftar.includes(x.textContent));
            if (cocok) return cocok.closest('[role="button"], button');
            bar = bar.parentElement;
        }
        return null;
    }
    return null;
}"""


async def _kartu(page: Page) -> list[dict[str, str]]:
    try:
        return await page.evaluate(_KARTU_JS)
    except Exception:
        return []


async def get_caption(page: Page, target: str) -> str:
    """Caption dari kartu utama di tampilan utas (cadangan: judul dokumen)."""
    kartu = await _kartu(page)
    if kartu:
        teks = _bersihkan_caption(kartu[0].get("teks", ""), _handle(target))
        if len(teks) > 3:
            return teks
    try:
        judul = await page.title()
        judul = re.sub(r"^\(\d+\)\s*", "", judul).strip()
        if judul and judul.lower() not in ("threads", "thread"):
            return judul
    except Exception:
        pass
    return ""


async def like_if_not_liked(page: Page) -> bool:
    """Like postingan utama & VERIFIKASI judul ikonnya berubah jadi Unlike."""
    for attempt in range(10):
        kartu = await _kartu(page)
        if kartu:
            break
        if attempt in (3, 6):
            await page.evaluate("window.scrollBy(0, 300)")
        await page.wait_for_timeout(1500)
    else:
        logger.info("Tombol Like postingan tidak ditemukan.")
        return False

    if kartu[0]["like"] in _JUDUL_UNLIKE:
        logger.info("Sudah di-like. Lewati.")
        return True
    handle = await page.evaluate_handle(_TOMBOL_KARTU_JS, ["|".join(_JUDUL_LIKE), 0])
    tombol = handle.as_element()
    if tombol is None:
        return False
    await tombol.scroll_into_view_if_needed()
    await tombol.click()
    for _ in range(12):
        await page.wait_for_timeout(500)
        kartu = await _kartu(page)
        if kartu and kartu[0]["like"] in _JUDUL_UNLIKE:
            logger.info("Berhasil like postingan.")
            return True
    logger.info("Like diklik, tetapi status aktif belum dapat diverifikasi.")
    return False


async def hitung_komentar_saya(page: Page, username: str) -> int:
    """Balasan di utas ini yang penulisnya persis ``username`` (kartu utama tidak dihitung)."""
    for _ in range(6):
        await page.evaluate("window.scrollBy(0, 600)")
        await page.wait_for_timeout(1000)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(600)
    kartu = await _kartu(page)
    nama = username.lower().lstrip("@")
    return sum(1 for k in kartu[1:] if k.get("penulis", "").lower() == nama)


async def kirim_satu_komentar(page: Page, text: str) -> bool:
    """Balas postingan utama lewat dialog komposer, lalu tunggu 'Posted'."""
    handle = await page.evaluate_handle(_TOMBOL_KARTU_JS, ["|".join(_JUDUL_REPLY), 0])
    tombol_balas = handle.as_element()
    if tombol_balas is None:
        logger.info("Tombol Reply postingan tidak ditemukan.")
        return False
    await tombol_balas.scroll_into_view_if_needed()
    await tombol_balas.click()

    dialog = page.locator('[role="dialog"]').last
    try:
        await dialog.wait_for(state="visible", timeout=15000)
    except Exception:
        logger.info("Dialog balasan Threads tidak terbuka.")
        return False
    kotak = dialog.locator('[contenteditable="true"]').first
    try:
        await kotak.wait_for(state="visible", timeout=10000)
    except Exception:
        await page.keyboard.press("Escape")
        return False
    await kotak.click()
    await page.wait_for_timeout(random.randint(300, 700))
    # Per karakter dengan jeda acak - sama seperti Instagram - supaya
    # kelihatan seperti diketik orang, bukan ditempel sekaligus.
    for ch in text:
        await page.keyboard.type(ch, delay=random.randint(50, 150))
    await page.wait_for_timeout(random.randint(500, 1000))

    dikirim = False
    for label in _TOMBOL_KIRIM:
        tombol = dialog.get_by_role("button", name=label, exact=True).last
        if await tombol.count() == 0:
            continue
        try:
            await tombol.click(timeout=5000)
            dikirim = True
            break
        except Exception:
            continue
    if not dikirim:
        await page.keyboard.press("Escape")
        logger.info("Tombol Post di dialog balasan tidak ditemukan.")
        return False

    try:
        await dialog.wait_for(state="detached", timeout=20000)
    except Exception:
        # Dialog yang tidak mau menutup = kiriman ditolak (biasanya dibatasi).
        await _pastikan_tidak_dibatasi(page)
        await page.keyboard.press("Escape")
        return False
    # Konfirmasi "Posted"; kalau tidak muncul, dialog yang menutup tanpa
    # pesan galat sudah cukup dianggap terkirim (balasan teks terbit seketika).
    for _ in range(8):
        status = await _teks_status(page)
        if re.search(r"\b(posted|diposting|terkirim)\b", status, re.IGNORECASE):
            break
        await page.wait_for_timeout(1000)
    await _pastikan_tidak_dibatasi(page)
    return True


# ============================================================
#  ALUR UTAMA - urutan & jeda SAMA dengan Instagram
# ============================================================


async def _process_posts(
    page: Page,
    username: str,
    target: str,
    comment_count: int,
    log: Any | None = None,
    max_posts: int | None = None,
    tone: str = "positif",
) -> dict[str, Any]:
    tone = normalize_tone(tone)
    username = username.lstrip("@")
    commented_posts = _load_commented_state(username)
    comments_posted = 0
    handle = _handle(target)

    if log:
        log(f"Membuka profil target @{handle}")
    await open_target_profile(page, target)
    await close_popups(page)
    await check_page_access(page)

    limit = max_posts if max_posts and max_posts > 0 else int(os.getenv("MAX_POSTS_PER_JOB", "10"))
    posts = await collect_posts(page, target, max(1, min(limit, 50)), log=log)
    if log:
        log(f"Ditemukan {len(posts)} postingan")

    for index, post in enumerate(posts, 1):
        url = post["url"]
        caption = post.get("caption") or ""
        saved_count = int(commented_posts.get(url, 0) or 0)
        if log:
            log(f"[{index}/{len(posts)}] Membuka {url}")

        await open_post(page, url, target)
        await check_page_access(page)
        await close_popups(page)
        await page.wait_for_timeout(1000)
        if not caption:
            caption = await get_caption(page, target)

        if log:
            log("  Menyukai postingan ...")
        liked = await like_if_not_liked(page)
        if log:
            log("  LIKE: " + ("OK (postingan disukai)" if liked else "gagal/tidak ditemukan"))

        existing_count = await hitung_komentar_saya(page, username)
        if saved_count > existing_count:
            existing_count = saved_count
        if log:
            log(f"  @{username} sudah {existing_count} balasan di sini (target {comment_count}).")

        if existing_count >= comment_count:
            if log:
                log("  Sudah mencapai target, lewati.")
            commented_posts[url] = existing_count
            _save_commented_state(username, commented_posts)
            continue

        need = comment_count - existing_count
        if log:
            log(f"  Perlu menambahkan {need} balasan lagi.")
        if not caption:
            if log:
                log(f"  Caption kosong, pakai komentar cadangan ({tone}).")
            comments_to_send = fallback_comments("caption tidak ditemukan", need, tone)
        else:
            if log:
                log(f"  Menyusun {need} komentar (tone: {tone})...")
            comments_to_send = await generate_ai_comments(
                caption, need, tone=tone, local_fallback=generate_comments_from_bank
            )
        comments_to_send = comments_to_send[:need]
        random.shuffle(comments_to_send)

        current_count = existing_count
        for i, teks in enumerate(comments_to_send, 1):
            if log:
                log(f"  Balasan #{i}: {teks[:70]}")
            ok = await kirim_satu_komentar(page, teks)
            if ok:
                current_count += 1
                comments_posted += 1
                if log:
                    log(f"    TERKIRIM (total @{username}: {current_count})")
            elif log:
                log("    GAGAL terkirim.")
            if i < len(comments_to_send):
                await page.wait_for_timeout(random.randint(3000, 6000))

        commented_posts[url] = current_count
        _save_commented_state(username, commented_posts)
        if log:
            log(f"  Total @{username}: {current_count} balasan di postingan ini.")
        await page.wait_for_timeout(int(random.uniform(DELAY_MIN, DELAY_MAX) * 1000))

    if log:
        log(f"Selesai: {comments_posted} balasan pada {len(posts)} postingan")
    return {
        "status": "success",
        "comments_posted": comments_posted,
        "posts_processed": len(posts),
    }
