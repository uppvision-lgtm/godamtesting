"""Threads uploader via browser session.

Tanpa API key. Autentikasi lewat cookie `sessionid` (DevTools -> Application
-> Cookies), atau lewat login browser di Account Manager.

Berbeda dengan Instagram, Threads menerima klik Playwright/CDP biasa, jadi
tidak perlu klik level OS dan tidak perlu jendela terlihat.

CATATAN PENTING soal selektor: JANGAN memakai pencocokan teks longgar seperti
`:has-text('Post')`. Setiap postingan di beranda punya tombol "Posting ulang"
(repost) yang ikut cocok, dan versi lama adapter ini benar-benar membuka menu
repost milik orang lain alih-alih komposer. Semua tombol di sini dicari dengan
nama PERSIS dan DI-SCOPE ke dalam dialog komposer.

Bahasa antarmuka ikut akun/peramban, jadi setiap label disediakan dalam dua
bahasa (Inggris dan Indonesia).
"""

import re
from pathlib import Path

from autoupload import tahap
from autoupload.browser import (
    MediaKind,
    PlatformError,
    PlaywrightPlatform,
    tautan_baru,
    tautan_profil,
)

# Kata status yang berarti postingan SUDAH terbit. Dicocokkan sebagai kata
# utuh: status "Posting..." muncul selama video masih diunggah dan tidak boleh
# tertukar dengan "Posted".
_SUDAH_TERBIT = re.compile(
    r"\b(posted|diposting|terkirim|dibagikan|dipublikasikan)\b", re.IGNORECASE
)

# Label tombol, EN + ID.
_BUKA_KOMPOSER = ("New thread", "Utas baru")
_KIRIM = ("Post", "Kirim")

_PEMILIH_POST = 'a[href*="/post/"]'

# Kolom tulis inline di atas beranda. aria-label-nya deskriptif dan stabil;
# dipakai lebih dulu karena tidak bergantung pada teks tombol sidebar.
_UMPAN_KOMPOSER = (
    '[aria-label^="Empty text field"]',
    '[aria-label^="Bidang teks kosong"]',
)


class Threads(PlaywrightPlatform):
    platform_key = "threads"
    # Threads pindah dari threads.net ke threads.com. Alamat baru dipakai
    # langsung supaya tidak ada satu pengalihan tambahan di setiap upload.
    start_url = "https://www.threads.com/"
    # Cookie harus dipasang di KEDUA domain: kalau hanya .threads.net, sesinya
    # hilang begitu pengalihan ke .threads.com terjadi dan tampak seperti mati.
    session_cookie_map = {"sessionid": [".threads.net", ".threads.com"]}

    async def _do_upload(
        self, page, media_path: Path, caption: str, media_kind: MediaKind, **options
    ) -> str | None:
        # Kemajuan dilaporkan dari titik-titik nyata, bukan ditebak
        # dari waktu: hanya di sinilah yang benar-benar diketahui.
        lapor = options.get("lapor") or (lambda *_: None)
        lapor(*tahap.MULAI)
        # Platform ini tidak punya penjadwalan bawaan. Ditolak di sini
        # supaya tidak diam-diam terbit SEKARANG padahal diminta nanti.
        if options.get("jadwal"):
            raise PlatformError(
                "Threads tidak punya penjadwalan di komposer web-nya. Hapus jadwalnya untuk mengirim sekarang, atau keluarkan Threads dari platform tujuan."
            )

        nama = str(options.get("nama_akun") or "").lstrip("@")
        url_profil = f"https://www.threads.com/@{nama}" if nama else ""
        # Daftar postingan sebelum upload, supaya tautan hasilnya dipastikan
        # yang baru dan bukan postingan lama di urutan atas.
        sebelum: set[str] = set()
        if url_profil:
            sebelum = await tautan_profil(page, url_profil, _PEMILIH_POST)

        await page.goto(self.start_url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(4_000)

        lapor(*tahap.HALAMAN)
        if "login" in page.url.lower():
            raise PlatformError(
                "Sesi Threads sudah tidak valid (ter-logout). Perbarui Session ID "
                "di halaman Auto Upload."
            )

        # 1. Buka komposer.
        dibuka = False
        for sel in _UMPAN_KOMPOSER:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            try:
                await loc.click(timeout=5_000)
                dibuka = True
                print(f"[threads] komposer dibuka lewat {sel}", flush=True)
                break
            except Exception:  # noqa: BLE001 - coba cara berikutnya
                continue
        if not dibuka:
            for label in _BUKA_KOMPOSER:
                tombol = page.get_by_role("button", name=label, exact=True).first
                if await tombol.count() == 0:
                    continue
                try:
                    await tombol.click(timeout=5_000)
                    dibuka = True
                    print(f"[threads] komposer dibuka lewat tombol {label!r}", flush=True)
                    break
                except Exception:  # noqa: BLE001 - coba label berikutnya
                    continue
        if not dibuka:
            raise PlatformError(
                "Tidak menemukan cara membuka komposer Threads (kolom "
                "'Apa yang baru?' maupun tombol 'Utas baru')."
            )

        dialog = page.locator('[role="dialog"]').last
        try:
            await dialog.wait_for(state="visible", timeout=20_000)
        except Exception as exc:  # noqa: BLE001
            raise PlatformError("Dialog komposer Threads tidak terbuka.") from exc
        lapor(*tahap.KOMPOSER)

        # 2. Tulis caption.
        kotak = dialog.locator('[contenteditable="true"]').first
        await kotak.wait_for(state="visible", timeout=20_000)
        await kotak.click()
        # type() dipakai, bukan fill(): kolomnya contenteditable milik React,
        # dan fill() tidak memicu event yang membuat tombol kirim aktif.
        await page.keyboard.type(caption[:500], delay=10)
        await page.wait_for_timeout(1_000)
        print("[threads] caption terisi", flush=True)
        lapor(*tahap.CAPTION_AWAL)

        # 3. Lampirkan media.
        berkas = dialog.locator('input[type="file"]').first
        try:
            await berkas.wait_for(state="attached", timeout=10_000)
            await berkas.set_input_files(str(media_path.resolve()), timeout=120_000)
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(f"Gagal melampirkan media ke Threads: {exc}") from exc
        lapor(*tahap.LAMPIR)

        # Tunggu sampai pratinjaunya benar-benar muncul. Tanpa ini, tombol
        # kirim bisa ditekan saat media belum selesai diproses dan yang
        # terkirim hanya teksnya.
        pratinjau = dialog.locator('video, img[src^="blob:"], img[src^="data:"]').first
        try:
            await pratinjau.wait_for(state="visible", timeout=180_000)
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(
                "Media sudah dilampirkan tetapi pratinjaunya tidak pernah muncul; "
                "postingan dibatalkan supaya tidak terkirim tanpa video."
            ) from exc
        print("[threads] media terlampir", flush=True)
        lapor(*tahap.MEDIA_SIAP)

        # 4. Kirim. Tombolnya DI-SCOPE ke dialog: di luar dialog ada tombol
        #    "Post" milik kolom inline, dan di beranda ada "Posting ulang".
        terkirim = False
        for label in _KIRIM:
            tombol = dialog.get_by_role("button", name=label, exact=True).last
            if await tombol.count() == 0:
                continue
            try:
                await tombol.click(timeout=10_000)
                terkirim = True
                lapor(*tahap.KIRIM)
                print(f"[threads] tombol {label!r} ditekan", flush=True)
                break
            except Exception:  # noqa: BLE001 - coba label berikutnya
                continue
        if not terkirim:
            raise PlatformError(
                "Tombol kirim Threads tidak ditemukan - postingan tidak terkirim."
            )

        # 5. JANGAN pakai "dialog tertutup" sebagai tanda berhasil. Dialog
        #    menutup seketika setelah tombol kirim ditekan, padahal videonya
        #    baru diunggah SESUDAH itu (permintaan rupload_igvideo, sekitar
        #    30-45 detik). Menutup peramban di titik itu membatalkan unggahan
        #    dan postingannya tidak pernah terbit - persis yang terjadi
        #    sebelum perbaikan ini. Yang ditunggu adalah pemberitahuan
        #    "Posted"/"Diposting" yang muncul setelah unggahan selesai.
        try:
            await dialog.wait_for(state="detached", timeout=60_000)
        except Exception:  # noqa: BLE001 - sebagian tampilan hanya menyembunyikan
            pass

        lapor(*tahap.TUNGGU)
        if not await self._tunggu_terbit(page):
            raise PlatformError(
                "Tombol kirim sudah ditekan tetapi Threads tidak pernah "
                "memberi konfirmasi terbit dalam 3 menit; status postingan "
                "tidak bisa dipastikan."
            )
        print("[threads] terkirim", flush=True)

        if url_profil:
            tautan = await tautan_baru(page, url_profil, _PEMILIH_POST, sebelum)
            if tautan:
                print(f"[threads] tautan: {tautan}", flush=True)
                return tautan
        return url_profil or self.start_url

    async def _tunggu_terbit(self, page) -> bool:
        """Tunggu Threads memberi tahu bahwa postingannya sudah terbit.

        Threads memakai daerah status untuk melaporkan kemajuan: "Posting..."
        selama video diunggah, lalu "Posted" ketika selesai. Kata dasarnya
        sama, jadi pencocokannya harus pada kata UTUH - "Posting" tidak boleh
        dianggap "Posted", kalau tidak peramban ditutup terlalu cepat lagi.
        """
        for _ in range(90):  # ~3 menit
            teks = await page.evaluate(
                """() => [...document.querySelectorAll('[role="alert"], [role="status"]')]
                        .map(e => (e.innerText || '').trim())
                        .filter(Boolean).join(' | ')"""
            )
            if teks and _SUDAH_TERBIT.search(teks):
                return True
            await page.wait_for_timeout(2_000)
        return False
