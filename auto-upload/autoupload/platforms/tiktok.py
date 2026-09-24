"""TikTok uploader lewat TikTok Studio (tiktok.com/tiktokstudio/upload).

Catatan yang didapat dari pengamatan langsung, bukan tebakan:

- Kotak caption sudah TERISI nama berkas begitu video dilampirkan
  (mis. "uji-upload"). Kalau tidak dikosongkan, caption kita menempel di
  belakang nama berkas itu. Editornya Draft.js, dan fill("") tidak
  membersihkannya - harus select-all lalu hapus lewat papan ketik.
- Ada popup "Fitur pengeditan baru ditambahkan" (tombol "Mengerti") yang
  menutupi tombol Posting, dan bisa muncul lagi di tengah proses.
- JANGAN memakai :has-text("Post"): itu ikut cocok dengan menu sidebar
  "Postingan" dan mengklik tombol yang salah. Dipakai data-e2e, atau nama
  tombol yang PERSIS.
- Tombol Posting dinonaktifkan TikTok selama video masih diproses, jadi
  ":not([disabled])" adalah penanda siap yang dipakai TikTok sendiri.
"""

import re
import time
from datetime import timedelta
from pathlib import Path

from autoupload import tahap
from autoupload.browser import (
    MediaKind,
    PlatformError,
    PlaywrightPlatform,
    _tautan_di_halaman,
    tautan_baru,
    waktu_lokal_halaman,
)


async def _ditolak_login(page) -> bool:
    """True kalau yang muncul halaman login, bukan halaman milik akun."""
    alamat = (page.url or "").lower()
    if "/login" in alamat or "/auth/" in alamat:
        return True
    return bool(await page.get_by_role("button", name="Log in", exact=True).count())

# Popup informasi yang bisa menutupi tombol Posting.
# "Batal" SENGAJA tidak ada di sini: itu tombol pada dialog konfirmasi
# posting, dan menutup popup dengannya akan membatalkan unggahan.
_TUTUP_POPUP = ("Mengerti", "Got it", "OK", "Oke", "Nanti", "Lain kali")

# Dialog "Lanjut posting?" muncul kalau pemeriksaan hak cipta belum selesai
# saat tombol Posting ditekan. Tanpa menekan tombol ini, prosesnya menggantung.
_LANJUT_POSTING = ("Posting sekarang", "Post now")

# Teks yang berarti unggahan sudah diterima TikTok.
_BERHASIL = re.compile(
    r"(berhasil|sedang diunggah|dibagikan|success|posted|kelola postingan|manage your posts)",
    re.IGNORECASE,
)

# Keluhan yang muncul di blok jadwal kalau waktunya tidak diterima. TikTok
# menuntut minimal 15 menit dari sekarang dan maksimal 10 hari ke depan.
_JADWAL_DITOLAK = re.compile(
    r"(minimal\s*\d+\s*menit|at least\s*\d+\s*minutes|maksimal|at most|"
    r"tidak valid|invalid)", re.IGNORECASE
)

_PEMILIH_POST = 'a[href*="/video/"]'

# Daftar postingan dibaca dari TikTok Studio, BUKAN dari profil publik:
# membuka profil publik dengan sesi ini justru memuat halaman notifikasi,
# dan tautan video yang terbaca di situ milik orang lain.
_URL_DAFTAR = "https://www.tiktok.com/tiktokstudio/content"
# Daftarnya dirender belakangan; 5 detik bawaan belum cukup.
_JEDA_DAFTAR = 10_000


class TikTok(PlaywrightPlatform):
    platform_key = "tiktok"
    # TikTok memindahkan pengunggah ke "TikTok Studio"; URL /upload lama 404.
    start_url = "https://www.tiktok.com/tiktokstudio/upload?lang=en"
    session_cookie_map = {"sessionid": ".tiktok.com"}
    # TikTok menyimpan sesi di pusat data tempat ia dibuat dan memilih pusat
    # data dari IP pemanggil. Sesi yang dibuat di Indonesia (alisg, Singapura)
    # karena itu ditolak begitu dipakai dari server Amerika - halamannya
    # melempar ke /login walau cookie-nya masih hidup. Urutannya dari yang
    # paling sering dipakai pengguna di sini.
    wilayah_cookie = "tt-target-idc"
    wilayah_kandidat = ("alisg", "useast2a", "useast5", "maliva", "useast1a")
    # jenis_diblokir SENGAJA dibiarkan kosong di sini, walau X memakainya
    # dan halaman TikTok Studio sama-sama penuh sampul video. Dicoba pada 24
    # September 2026 dan hasilnya LEBIH BURUK: proses driver Playwright
    # (Node) mati dengan "Error: Invalid InterceptionId", dan unggahannya
    # berhenti di "membuka halaman" - lebih awal daripada tanpa pemblokiran.
    # Sebabnya TikTok memanggil page.goto berkali-kali saat mencari wilayah
    # data, dan membatalkan permintaan yang keburu hilang di tengah navigasi
    # membuat driver-nya menegaskan lalu mati. X selamat karena hanya sekali
    # navigasi. Jangan dipasang lagi tanpa menangani unroute per navigasi.
    #
    # Headless LAMA tetap dipakai, seperti X: headless bawaan menjalankan
    # tumpukan peramban lengkap, dan di kontainer 954 MB itulah yang membuat
    # pemakaian menyentuh batas.
    extra_args = ("--headless=old",)

    async def _do_upload(
        self, page, media_path: Path, caption: str, media_kind: MediaKind, **options: object
    ) -> str | None:
        # Kemajuan dilaporkan dari titik-titik nyata, bukan ditebak
        # dari waktu: hanya di sinilah yang benar-benar diketahui.
        lapor = options.get("lapor") or (lambda *_: None)
        lapor(*tahap.MULAI)
        jadwal = options.get("jadwal")
        nama = str(options.get("nama_akun") or "").lstrip("@")
        url_profil = f"https://www.tiktok.com/@{nama}" if nama else ""
        # Halaman daftar sekaligus penentu wilayah data: kalau ditolak, cookie
        # wilayah dicoba satu per satu di sini, sehingga halaman unggah di
        # bawah sudah pasti memakai wilayah yang benar.
        # Kemajuan dilaporkan di SETIAP langkah panjang di bawah ini. Dulu
        # tidak: sesudah MULAI (5%) laporan berikutnya baru LAMPIR (50%),
        # padahal di antaranya ada pencarian wilayah data sampai lima kali,
        # pemuatan halaman unggah, dan pelampiran berkas - menit-menit yang
        # di layar tampak sebagai satu angka 5% yang tidak bergerak. Waktunya
        # ikut dicatat ke log supaya kalau melambat, ketahuan langkah mana.
        t0 = time.monotonic()
        wilayah = await self.buka_masuk(page, _URL_DAFTAR, _ditolak_login, _JEDA_DAFTAR)
        print(f"[tiktok] masuk: {time.monotonic() - t0:.1f}s (wilayah={wilayah})", flush=True)
        if wilayah is None:
            raise PlatformError(
                "Sesi TikTok sudah tidak valid (ter-logout). Perbarui Session ID "
                "di halaman Kelola Akun."
            )
        lapor(*tahap.HALAMAN)
        sebelum = set(await _tautan_di_halaman(page, _PEMILIH_POST))

        t0 = time.monotonic()
        await page.goto(self.start_url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(6_000)
        print(f"[tiktok] halaman unggah: {time.monotonic() - t0:.1f}s", flush=True)

        if await _ditolak_login(page):
            raise PlatformError(
                "Sesi TikTok sudah tidak valid (halaman login muncul). Perbarui "
                "Session ID di halaman Kelola Akun."
            )
        lapor(*tahap.KOMPOSER)

        # 1. Lampirkan berkas.
        t0 = time.monotonic()
        await self._lampirkan(page, media_path)
        print(f"[tiktok] berkas dikirim: {time.monotonic() - t0:.1f}s", flush=True)

        # Kotak caption baru ada setelah berkasnya diterima - itu sekaligus
        # tanda lampirannya masuk.
        kotak = page.locator('div[contenteditable="true"].public-DraftEditor-content').first
        t0 = time.monotonic()
        try:
            await kotak.wait_for(state="visible", timeout=120_000)
        except Exception as exc:  # noqa: BLE001
            # Sebab aslinya WAJIB ikut. Tanpa ini "editor caption tidak
            # muncul" terbaca sebagai berkas ditolak, padahal habisnya waktu
            # (TikTok memang lambat) dan perambannya MATI (kontainer kehabisan
            # memori) adalah dua hal yang sama sekali berbeda - dan yang kedua
            # gagal seketika, bukan setelah 120 detik.
            sebab = f"{type(exc).__name__}: {str(exc).splitlines()[0][:100]}"
            keadaan = await self._keadaan(page)
            print(f"[tiktok] editor caption TIDAK MUNCUL ({sebab}) {keadaan}", flush=True)
            raise PlatformError(
                f"TikTok tidak pernah menampilkan editor caption. {sebab} {keadaan}"
            ) from exc
        print(f"[tiktok] media terlampir: {time.monotonic() - t0:.1f}s", flush=True)
        lapor(*tahap.LAMPIR)

        await self._tutup_popup(page)

        # 2. Caption. Kosongkan dulu isian otomatis (nama berkas).
        await kotak.click()
        await page.keyboard.press("ControlOrMeta+a")
        await page.keyboard.press("Backspace")
        await page.wait_for_timeout(500)
        await page.keyboard.type(caption[:2_200], delay=10)
        await page.wait_for_timeout(1_000)
        terbaca = (await kotak.inner_text() or "").strip()
        print(f"[tiktok] caption terisi: {terbaca[:60]!r}", flush=True)
        lapor(*tahap.CAPTION)

        # 3. Jadwal bawaan TikTok, kalau diminta.
        if jadwal:
            lapor(*tahap.JADWAL)
            await self._pasang_jadwal(page, jadwal)

        # 4. Tunggu tombol Posting aktif, sambil menutup popup yang muncul lagi.
        tombol = None
        for _ in range(60):  # ~5 menit
            await self._tutup_popup(page)
            kandidat = page.locator(
                'button[data-e2e="post_video_button"]:not([disabled]), '
                'button[data-e2e="share_btn"]:not([disabled])'
            ).first
            if await kandidat.count():
                tombol = kandidat
                break
            await page.wait_for_timeout(5_000)
        if tombol is None:
            # Ambil keterangan yang TikTok tampilkan sendiri. Tanpa ini yang
            # sampai ke pengguna hanya jejak timeout Playwright, yang tidak
            # menjelaskan apa pun.
            alasan = await page.evaluate(
                """() => [...document.querySelectorAll('*')]
                    .filter(e => !e.children.length)
                    .map(e => (e.innerText || '').trim())
                    .filter(t => t && /minimal|at least|maksimal|at most|tidak valid|invalid|gagal|error/i.test(t))
                    .slice(0, 3).join('; ')"""
            )
            raise PlatformError(
                "Tombol Posting TikTok tidak pernah aktif"
                + (f": {alasan}" if alasan else
                   " - videonya mungkin masih diproses atau ditolak.")
            )

        # 4. Kirim.
        await tombol.scroll_into_view_if_needed()
        await tombol.click(timeout=15_000)
        lapor(*tahap.KIRIM)
        print("[tiktok] tombol Posting ditekan", flush=True)

        # 5. Tunggu TikTok mengakui unggahannya. JANGAN langsung pindah ke
        #    profil: berpindah halaman saat unggahan masih berjalan bisa
        #    membatalkannya.
        lapor(*tahap.TUNGGU)
        if not await self._tunggu_diterima(page):
            raise PlatformError(
                "Tombol Posting sudah ditekan tetapi TikTok tidak pernah "
                "mengakui unggahannya; status postingan tidak bisa dipastikan."
            )
        print("[tiktok] unggahan diterima", flush=True)

        # Postingan terjadwal belum terbit, jadi tidak ada tautan untuk dicari.
        if jadwal:
            return "https://www.tiktok.com/tiktokstudio/content"

        # 6. Tautan postingannya.
        tautan = await tautan_baru(
            page, _URL_DAFTAR, _PEMILIH_POST, sebelum, jeda_ms=_JEDA_DAFTAR
        )
        if tautan:
            print(f"[tiktok] tautan: {tautan}", flush=True)
            return tautan
        return url_profil or "https://www.tiktok.com/"

    @staticmethod
    async def _pasang_jadwal(page, kapan) -> None:
        """Pilih "Jadwalkan" pada blok "When to post", lalu isi tanggal & jamnya.

        Tiga hal yang mudah salah dan sudah dibuktikan lewat pengamatan:

        - Radionya dipilih lewat NILAI (value != "post_now"), bukan lewat teks
          "Schedule": teksnya ikut berganti kalau bahasa antarmuka berubah.
        - Memilihnya memunculkan dialog persetujuan "Izinkan video Anda
          disimpan untuk posting terjadwal?". Selama dialog itu belum
          disetujui, radionya TIDAK tercentang dan kolom tanggalnya tidak
          pernah muncul.
        - Kolomnya `input[type=text]`, bukan input tanggal/jam bawaan
          peramban, jadi dikenali dari POLA nilainya (jam "HH:MM" versus
          tanggal "YYYY-MM-DD"), bukan dari urutan di DOM.
        """
        kapan = await waktu_lokal_halaman(page, kapan)
        kotak = page.locator('[data-e2e="schedule_container"]').first
        if await kotak.count() == 0:
            raise PlatformError(
                "Blok 'When to post' tidak ada di halaman unggah TikTok, jadi "
                "jadwalnya tidak bisa dipasang."
            )
        radio = kotak.locator('input[type="radio"]:not([value="post_now"])').first
        if await radio.count() == 0:
            raise PlatformError("Pilihan jadwal TikTok tidak ditemukan.")
        # Radio aslinya disembunyikan di balik lingkaran bergaya -> klik label.
        label = radio.locator("xpath=ancestor::label[1]")
        await (label.first if await label.count() else radio).click(timeout=10_000)
        await page.wait_for_timeout(2_000)

        for setuju in ("Izinkan", "Allow", "Setuju"):
            tombol = page.get_by_role("button", name=setuju, exact=True)
            if await tombol.count():
                await tombol.first.click(timeout=8_000)
                print(f"[tiktok] persetujuan jadwal diberikan lewat {setuju!r}", flush=True)
                break
        await page.wait_for_timeout(3_000)

        if not await radio.is_checked():
            raise PlatformError(
                "Pilihan 'Jadwalkan' TikTok tidak tercentang - dialog "
                "persetujuan penyimpanan video mungkin belum disetujui."
            )

        # Menitnya HARUS kelipatan 5: pemilih TikTok hanya menyediakan
        # 00,05,...,55. Dibulatkan ke kelipatan terdekat, dan kalau itu
        # membuatnya jatuh ke masa lalu, dibulatkan ke atas.
        menit = round(kapan.minute / 5) * 5
        kapan = kapan.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=menit)
        tgl, jam = kapan.strftime("%Y-%m-%d"), kapan.strftime("%H:%M")

        kolom = kotak.locator('input[type="text"]')
        kolom_jam = kolom_tgl = None
        for i in range(await kolom.count()):
            nilai = await kolom.nth(i).input_value() or ""
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", nilai):
                kolom_tgl = kolom.nth(i)
            elif re.fullmatch(r"\d{1,2}:\d{2}", nilai):
                kolom_jam = kolom.nth(i)
        if kolom_jam is None or kolom_tgl is None:
            raise PlatformError(
                "Kolom tanggal/jam jadwal TikTok tidak ditemukan; jadwalnya "
                "tidak bisa dipastikan."
            )

        # Kolomnya readonly - nilainya hanya bisa diubah lewat pemilih.
        await kolom_jam.click(timeout=8_000)
        await page.wait_for_timeout(1_500)
        await TikTok._pilih_jam(page, kapan.hour, kapan.minute)
        await page.wait_for_timeout(1_000)

        await kolom_tgl.click(timeout=8_000)
        await page.wait_for_timeout(1_500)
        await TikTok._pilih_tanggal(page, kapan.day)
        await page.wait_for_timeout(1_500)

        terbaca_tgl = await kolom_tgl.input_value()
        terbaca_jam = await kolom_jam.input_value()
        if (terbaca_tgl, terbaca_jam) != (tgl, jam):
            raise PlatformError(
                f"TikTok tidak menerima jadwalnya (diminta {tgl} {jam}, "
                f"terbaca {terbaca_tgl} {terbaca_jam})."
            )
        keluhan = await kotak.inner_text()
        if _JADWAL_DITOLAK.search(keluhan or ""):
            baris = next(
                (b.strip() for b in (keluhan or "").splitlines()
                 if _JADWAL_DITOLAK.search(b)),
                "waktunya tidak diterima",
            )
            raise PlatformError(f"TikTok menolak waktu jadwalnya: {baris}")
        print(f"[tiktok] jadwal dipasang: {tgl} {jam}", flush=True)

    @staticmethod
    async def _pilih_jam(page, jam: int, menit: int) -> None:
        """Pilih jam dan menit pada pemilih dua kolom.

        Kedua kolom memakai kelas yang sama, jadi dibedakan lewat posisi
        mendatar: yang kiri jam, yang kanan menit.
        """
        opsi = page.locator(".tiktok-timepicker-option-text")
        kotak = []
        for i in range(await opsi.count()):
            b = await opsi.nth(i).bounding_box()
            if b:
                kotak.append((b["x"], (await opsi.nth(i).inner_text()).strip(), i))
        if not kotak:
            raise PlatformError("Pemilih jam TikTok tidak muncul.")
        batas = (min(k[0] for k in kotak) + max(k[0] for k in kotak)) / 2
        for x, teks, i in kotak:
            kiri = x <= batas
            if kiri and teks == f"{jam:02d}":
                await opsi.nth(i).click(timeout=6_000)
                await page.wait_for_timeout(800)
                break
        for x, teks, i in kotak:
            if x > batas and teks == f"{menit:02d}":
                await opsi.nth(i).click(timeout=6_000)
                await page.wait_for_timeout(800)
                break

    @staticmethod
    async def _pilih_tanggal(page, hari: int) -> None:
        """Pilih tanggal pada kalender; hanya hari ber-kelas `valid` yang bisa.

        Hari dari bulan sebelah ikut ditampilkan, tetapi yang sudah lewat
        tidak ber-`valid`. TikTok hanya mengizinkan sepuluh hari ke depan,
        jadi kemunculan pertama angka yang cocok sudah pasti yang dimaksud.
        """
        hari_valid = page.locator(".day.valid")
        for i in range(await hari_valid.count()):
            if (await hari_valid.nth(i).inner_text()).strip() == str(hari):
                await hari_valid.nth(i).click(timeout=6_000)
                return
        raise PlatformError(
            f"Tanggal {hari} tidak bisa dipilih di kalender TikTok - "
            "TikTok hanya menerima jadwal sampai sepuluh hari ke depan."
        )

    async def _lampirkan(self, page, media_path: Path) -> None:
        """Serahkan berkasnya ke kotak unggah TikTok.

        Dulu satu baris: locator('input[type="file"]').first. Dua kelemahannya
        terbukti di server pada 24 September 2026, dan keduanya berakhir sama -
        "Locator.set_input_files: Timeout 120000ms exceeded" tanpa sepatah pun
        tentang APA yang sebenarnya ada di halaman:

        1. Kotak unggahnya bisa berada di dalam IFRAME. Pencarian di bingkai
           utama tidak akan pernah menemukannya, berapa lama pun ditunggu.
        2. Kalau TikTok justru menyodorkan tantangan keamanan atau halaman
           lain, tidak ada input berkas sama sekali - dan yang pantas
           dilaporkan adalah itu, bukan "timeout".

        Jadi: bingkai utama dulu dengan tunggu yang lebih pendek, lalu semua
        bingkai anak, dan kalau tetap nihil barulah menyerah - sambil membawa
        keadaan halamannya apa adanya.
        """
        pemilih = 'input[type="file"]'
        # Semua bingkai diperiksa BERSAMAAN tiap detik, bukan bingkai utama
        # sampai habis waktunya baru yang lain: kalau kotaknya memang di
        # iframe, menunggui bingkai utama hanya membuang menit.
        bingkai = await self._cari_kotak(page, pemilih, 45)
        if bingkai is not None:
            if bingkai is not page.main_frame:
                print(f"[tiktok] kotak unggah ada di iframe: {bingkai.url[:80]}", flush=True)
            await bingkai.locator(pemilih).first.set_input_files(
                str(media_path.resolve()), timeout=120_000
            )
            return

        keadaan = await self._keadaan(page)
        print(f"[tiktok] kotak unggah TIDAK ADA {keadaan}", flush=True)
        raise PlatformError(f"Kotak unggah TikTok tidak ada. {keadaan}")

    @staticmethod
    async def _cari_kotak(page, pemilih: str, batas_detik: float):
        """Bingkai pertama yang memuat `pemilih`, atau None kalau tidak ada."""
        habis = time.monotonic() + batas_detik
        while True:
            for bingkai in page.frames:
                try:
                    if await bingkai.locator(pemilih).count():
                        return bingkai
                except Exception:  # noqa: BLE001 - bingkai bisa hilang saat dibaca
                    continue
            if time.monotonic() >= habis:
                return None
            await page.wait_for_timeout(1_000)

    @staticmethod
    async def _keadaan(page) -> str:
        """Potret ringkas halaman, untuk ikut ke pesan galat dan ke log.

        Screenshot kegagalan memang disimpan, tetapi hanya bisa dilihat lewat
        halaman admin. Ringkasan teks ini ikut ke kolom galat barisnya dan ke
        log Railway, sehingga penyebabnya bisa dibaca tanpa membuka apa pun.
        """
        try:
            judul = (await page.title() or "")[:60]
            teks = await page.evaluate(
                "() => (document.body ? document.body.innerText : '')"
                ".replace(/\\s+/g, ' ').trim().slice(0, 200)"
            )
            return (
                f"[url={page.url[:90]} | judul={judul} | "
                f"iframe={len(page.frames) - 1} | isi=\"{teks}\"]"
            )
        except Exception as exc:  # noqa: BLE001 - diagnosa tidak boleh menutupi galat asli
            # Halaman yang tidak bisa ditanyai judulnya berarti perambannya
            # sudah tidak ada. Di kontainer ini penyebabnya hampir selalu
            # memori, jadi angkanya ikut dibawa - tanpa itu kegagalannya cuma
            # bisa ditebak.
            from autoupload.uploader import _keadaan_memori

            return (
                f"[peramban sudah mati: {type(exc).__name__} | "
                f"memori: {_keadaan_memori() or 'tidak terbaca'}]"
            )

    @staticmethod
    async def _tutup_popup(page) -> None:
        """Tutup popup informasi yang menghalangi tombol Posting.

        Keberadaannya dicek dengan count() dulu supaya tidak membayar timeout
        penuh untuk label yang memang tidak ada - fungsi ini dipanggil
        berulang selama menunggu.
        """
        for label in _TUTUP_POPUP:
            try:
                t = page.get_by_role("button", name=label, exact=True)
                if await t.count() == 0:
                    continue
                await t.first.click(timeout=2_000)
                print(f"[tiktok] popup ditutup lewat {label!r}", flush=True)
                await page.wait_for_timeout(600)
            except Exception:  # noqa: BLE001 - popup opsional
                pass

    @staticmethod
    async def _konfirmasi_lanjut(page) -> bool:
        """Setujui dialog "Lanjut posting?" kalau muncul."""
        for label in _LANJUT_POSTING:
            try:
                t = page.get_by_role("button", name=label, exact=True)
                if await t.count() == 0:
                    continue
                await t.first.click(timeout=5_000)
                print(f"[tiktok] dialog konfirmasi disetujui lewat {label!r}", flush=True)
                await page.wait_for_timeout(1_500)
                return True
            except Exception:  # noqa: BLE001 - dialog opsional
                pass
        return False

    @classmethod
    async def _tunggu_diterima(cls, page) -> bool:
        for _ in range(60):  # ~3 menit
            # Dialognya bisa telat muncul, jadi diperiksa tiap putaran.
            await cls._konfirmasi_lanjut(page)
            try:
                keadaan = await page.evaluate(
                    """() => ({
                        url: location.href,
                        teks: (document.body.innerText || '').replace(/\\s+/g, ' '),
                    })"""
                )
            except Exception:  # noqa: BLE001 - navigasi di tengah evaluate
                await page.wait_for_timeout(3_000)
                continue
            if "/upload" not in keadaan["url"]:
                return True
            if _BERHASIL.search(keadaan["teks"] or ""):
                return True
            await page.wait_for_timeout(3_000)
        return False
