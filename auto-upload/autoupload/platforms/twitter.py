"""X / Twitter uploader via Playwright - tanpa API berbayar.

X memakai atribut `data-testid` yang stabil dan tidak ikut berubah bahasa,
jadi selektornya aman. Yang perlu hati-hati justru soal WAKTU:

- Tombol kirim aktif (aria-disabled hilang) hanya setelah video selesai
  diunggah. Sebelum itu toast menunjukkan "nama.mp4: Uploading (0%)", lalu
  berubah menjadi "Ready".
- `[role="progressbar"]` TIDAK bisa dipakai sebagai penanda selesai: X selalu
  punya beberapa elemen itu di halamannya, bahkan saat menganggur.
- Sesudah kirim, konfirmasinya adalah toast "Your post was sent".

Semua ini diamati langsung, bukan ditebak.
"""

import re
from pathlib import Path

from autoupload.browser import (
    MediaKind,
    PlatformError,
    PlaywrightPlatform,
    tautan_baru,
    tautan_profil,
    waktu_lokal_halaman,
)

# Toast yang berarti postingan sudah benar-benar terkirim, EN + ID.
_TERKIRIM = re.compile(
    r"(was sent|will be sent|telah dikirim|berhasil dikirim|akan dikirim)", re.I
)
# Toast kegagalan; diperiksa lebih dulu supaya gagal tidak terbaca berhasil.
_GAGAL = re.compile(r"(failed|gagal|tidak terkirim|something went wrong)", re.I)
# Media masih diunggah. Ketidakhadiran inilah penanda selesai - BUKAN
# kehadiran kata "Ready", sebab toast itu sementara dan bisa keburu hilang
# sebelum sempat terbaca.
_SEDANG_UNGGAH = re.compile(r"(uploading|mengunggah|processing|memproses)", re.I)

_PEMILIH_POST = 'a[href*="/status/"]'


class Twitter(PlaywrightPlatform):
    platform_key = "twitter"
    start_url = "https://x.com/compose/post"
    # Session ID: cookie "auth_token" saja sudah cukup untuk masuk.
    session_cookie_map = {"auth_token": ".x.com"}

    async def _do_upload(
        self, page, media_path: Path, caption: str, media_kind: MediaKind, **options
    ) -> str | None:
        nama = str(options.get("nama_akun") or "").lstrip("@")
        url_profil = f"https://x.com/{nama}" if nama else ""

        # Daftar postingan sebelum upload, supaya tautan hasilnya bisa
        # dipastikan yang baru - bukan postingan tersemat di urutan atas.
        sebelum: set[str] = set()
        if url_profil:
            sebelum = await tautan_profil(page, url_profil, _PEMILIH_POST)

        await page.goto(self.start_url, wait_until="domcontentloaded", timeout=60_000)
        try:
            await page.wait_for_selector('[data-testid="tweetTextarea_0"]', timeout=30_000)
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(
                "Komposer X tidak terbuka. Sesinya mungkin sudah tidak valid - "
                "perbarui Session ID di halaman Auto Upload."
            ) from exc

        jadwal = options.get("jadwal")

        # 1. Caption. X memotong di 280 karakter.
        await page.click('[data-testid="tweetTextarea_0"]')
        await page.keyboard.type(caption[:280], delay=10)
        await page.wait_for_timeout(1_000)

        # 2. Lampirkan media.
        await page.locator('input[data-testid="fileInput"]').first.set_input_files(
            str(media_path.resolve()), timeout=180_000
        )
        try:
            await page.wait_for_selector('[data-testid="attachments"]', timeout=180_000)
        except Exception as exc:  # noqa: BLE001
            raise PlatformError("Media tidak pernah terlampir di komposer X.") from exc

        # 3. Tunggu unggahan media selesai. Menekan kirim lebih awal membuat
        #    X menolak, atau mengirim tanpa video.
        if not await self._tunggu_siap(page):
            raise PlatformError(
                "Video belum selesai diunggah ke X setelah 5 menit; "
                "postingan dibatalkan supaya tidak terkirim tanpa video."
            )
        print("[x] media siap", flush=True)

        # 4. Jadwal bawaan X, kalau diminta. Harus DIPASANG SEBELUM kirim:
        #    setelah ditekan, postingannya sudah terlanjur terbit.
        if jadwal:
            await self._pasang_jadwal(page, jadwal)

        # 5. Kirim.
        await page.click('[data-testid="tweetButton"]')

        # 6. Tunggu konfirmasi, bukan sekadar jeda tetap.
        if not await self._tunggu_terkirim(page):
            raise PlatformError(
                "Tombol kirim sudah ditekan tetapi X tidak pernah memberi "
                "konfirmasi terkirim; status postingan tidak bisa dipastikan."
            )
        print("[x] terkirim", flush=True)

        # Postingan terjadwal BELUM terbit, jadi tidak ada tautan yang bisa
        # dicari di profil. Mencarinya hanya akan menunggu sia-sia lalu
        # mengembalikan tautan postingan lama.
        if jadwal:
            return "https://x.com/compose/post/unsent/scheduled"

        # 7. Ambil tautan postingannya.
        if url_profil:
            tautan = await tautan_baru(page, url_profil, _PEMILIH_POST, sebelum)
            if tautan:
                print(f"[x] tautan: {tautan}", flush=True)
                return tautan
        return url_profil or "https://x.com/"

    @staticmethod
    async def _pasang_jadwal(page, kapan) -> None:
        """Pakai penjadwal bawaan X: tombol jam -> tanggal & waktu -> Confirm.

        Kolom `input[type=date]` dan `input[type=time]` dipakai langsung, bukan
        enam `<select>` di atasnya: mengisinya terbukti ikut memperbarui
        select-select itu, jadi React benar-benar menerima nilainya.
        """
        kapan = await waktu_lokal_halaman(page, kapan)
        await page.click('[data-testid="scheduleOption"]')
        await page.wait_for_timeout(2_500)

        tanggal = page.locator('input[type="date"]').first
        waktu = page.locator('input[type="time"]').first
        try:
            await tanggal.wait_for(state="visible", timeout=10_000)
        except Exception as exc:  # noqa: BLE001
            raise PlatformError("Panel jadwal X tidak terbuka.") from exc

        await tanggal.fill(kapan.strftime("%Y-%m-%d"))
        await waktu.fill(kapan.strftime("%H:%M"))
        await page.wait_for_timeout(1_000)

        terbaca = f"{await tanggal.input_value()} {await waktu.input_value()}"
        diminta = kapan.strftime("%Y-%m-%d %H:%M")
        if terbaca != diminta:
            raise PlatformError(
                f"X tidak menerima waktu jadwalnya (diminta {diminta}, "
                f"terbaca {terbaca})."
            )

        await page.click('[data-testid="scheduledConfirmationPrimaryAction"]')
        await page.wait_for_timeout(2_000)
        print(f"[x] jadwal dipasang: {diminta}", flush=True)

    @staticmethod
    async def _toast(page) -> str:
        return await page.evaluate(
            """() => [...document.querySelectorAll('[data-testid="toast"], [role="alert"]')]
                    .map(e => (e.innerText || '')).join(' | ')"""
        )

    async def _tunggu_siap(self, page) -> bool:
        """Tombol kirim sudah aktif dan tidak ada unggahan yang berjalan."""
        for _ in range(150):  # ~5 menit
            aktif = await page.evaluate(
                """() => {
                    const b = [...document.querySelectorAll('[data-testid="tweetButton"]')];
                    return b.length > 0 && b.some(x => x.getAttribute('aria-disabled') !== 'true');
                }"""
            )
            if aktif and not _SEDANG_UNGGAH.search(await self._toast(page)):
                return True
            await page.wait_for_timeout(2_000)
        return False

    async def _tunggu_terkirim(self, page) -> bool:
        for _ in range(90):  # ~3 menit
            toast = await self._toast(page)
            if toast:
                if _GAGAL.search(toast):
                    raise PlatformError(f"X menolak postingan: {toast.strip()[:160]}")
                if _TERKIRIM.search(toast):
                    return True
            await page.wait_for_timeout(2_000)
        return False
