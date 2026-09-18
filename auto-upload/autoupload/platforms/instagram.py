"""Instagram uploader via browser session (web create-post flow).

CATATAN PENTING (terbukti lewat pengujian nyata, 2026-09-18):
IG melakukan "trust check" pada TOMBOL ENTRY, yaitu tombol Buat (+) di sidebar.
Yang ditolaknya adalah event MOUSE sintetis: locator.click(), page.mouse
down/up, focus+Enter, maupun dispatchEvent semuanya diabaikan diam-diam tanpa
error dan tanpa permintaan jaringan.

Yang DITERIMA adalah event SENTUH. Dengan konteks berlayar sentuh
(has_touch=True) lalu tap(), komposer terbuka normal - termasuk dalam mode
headless. Karena itu upload Instagram tidak lagi butuh jendela terlihat,
pyautogui, izin Accessibility macOS, ataupun kursor yang bergerak, dan kini
bisa dijalankan di server Linux seperti platform lain.

Setelah komposer terbuka, semua langkah berikutnya (file chooser, Pangkas,
caption, Bagikan) dikendalikan lewat Playwright biasa.
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

# Label tombol entry + baris popover, EN + ID
_PLUS_LABELS = ("Buat", "Create", "New post", "Postingan baru")

# Video diposting sebagai reel, foto sebagai /p/ - keduanya dicari.
_PEMILIH_POST = 'a[href*="/reel/"], a[href*="/p/"]'


async def _tap(page, target, label: str) -> None:
    """Ketuk dengan event sentuh - satu-satunya yang diterima tombol entry IG.

    `target` boleh sebuah Locator, atau dict bounding box untuk elemen yang
    hanya diketahui posisinya (mis. baris popover hasil pemindaian).
    """
    if isinstance(target, dict):
        x = target["x"] + target["width"] / 2
        y = target["y"] + target["height"] / 2
        await page.touchscreen.tap(x, y)
    else:
        await target.tap(timeout=10_000)
    print(f"[ig] tap {label}", flush=True)


class Instagram(PlaywrightPlatform):
    platform_key = "instagram"
    # Enter via the home feed and click the nav "Create" (+) button: the
    # direct /create/ route no longer opens the composer for all accounts
    # (it can render a profile page instead).
    start_url = "https://www.instagram.com/"
    # Session ID shortcut: the Instagram "sessionid" browser cookie.
    session_cookie_map = {"sessionid": ".instagram.com"}
    # Konteks harus punya layar sentuh: tombol Buat (+) mengabaikan event
    # mouse sintetis dan hanya menanggapi event sentuh. Tanpa ini Playwright
    # juga menolak memanggil tap() sama sekali.
    has_touch = True

    async def _do_upload(
        self, page, media_path: Path, caption: str, media_kind: MediaKind, **options: object
    ) -> str | None:
        # Kemajuan dilaporkan dari titik-titik nyata, bukan ditebak
        # dari waktu: hanya di sinilah yang benar-benar diketahui.
        lapor = options.get("lapor") or (lambda *_: None)
        lapor(*tahap.MULAI)
        # Platform ini tidak punya penjadwalan bawaan. Ditolak di sini
        # supaya tidak diam-diam terbit SEKARANG padahal diminta nanti.
        if options.get("jadwal"):
            raise PlatformError(
                "Instagram tidak punya penjadwalan di komposer web-nya. Menjadwalkan postingan Instagram butuh akun profesional lewat Meta Business Suite. Hapus jadwalnya untuk mengirim sekarang, atau keluarkan Instagram dari platform tujuan."
            )

        nama = str(options.get("nama_akun") or "").lstrip("@")
        url_profil = f"https://www.instagram.com/{nama}/" if nama else ""
        sebelum: set[str] = set()
        if url_profil:
            sebelum = await tautan_profil(page, url_profil, _PEMILIH_POST)

        await page.goto(self.start_url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(4_000)
        lapor(*tahap.HALAMAN)

        # 0. Make sure we are actually logged in (otherwise the flow below is
        # doomed and the user gets a confusing error).
        login_form = await page.locator(
            'input[name="password"], form:has(input[name="username"])'
        ).count()
        if login_form or "/login" in page.url or "/accounts/login" in page.url:
            raise PlatformError(
                "Sesi Instagram sudah tidak valid (ter-logout). "
                "Perbarui Session ID di Account Manager."
            )
        logged_in = await page.locator('svg[aria-label*="post" i], svg[aria-label*="Postingan"]').count()
        if logged_in == 0:
            # Double check with the profile/avatar heuristics before failing.
            avatar = await page.locator('img[alt*="profile" i], img[alt*="foto" i]').count()
            if avatar == 0:
                raise PlatformError(
                    "Sesi Instagram sudah tidak valid (ter-logout). "
                    "Login ulang lewat Account Manager → Login Browser."
                )

        async def dismiss_dialogs() -> None:
            """Tutup dialog pemberitahuan yang menghalangi wizard.

            Keberadaan tombol dicek dengan count() lebih dulu, bukan langsung
            click(): fungsi ini dipanggil tiap putaran wizard, dan membayar
            timeout penuh untuk setiap label yang memang tidak ada akan
            membuat satu putaran memakan belasan detik.

            "OK"/"Mengerti" wajib ada di daftar: sejak video diposting sebagai
            reel, Instagram menyelipkan dialog "Postingan video sekarang
            dibagikan sebagai reel" DI ATAS layar Pangkas, dan dialog itu
            menutupi tombol Selanjutnya sampai diklik.
            """
            for label in (
                "OK", "Oke", "Mengerti", "Got it",
                "Not now", "Nanti", "Bukan sekarang", "Lain Kali",
                "Turn On", "Nyalakan", "Selesai", "Done",
            ):
                try:
                    tombol = page.get_by_role("button", name=label, exact=True)
                    if await tombol.count() == 0:
                        continue
                    await tombol.first.click(timeout=2_000)
                    print(f"[ig] dialog ditutup lewat tombol {label!r}", flush=True)
                    await page.wait_for_timeout(600)
                except Exception:  # noqa: BLE001 - dialog optional
                    pass

        await dismiss_dialogs()

        # 1. Buka komposer lewat tombol Buat (+) di navigasi. Navigasinya
        #    dirender belakangan, jadi ditunggu. Tombol ini di-trust-check
        #    Instagram: harus event SENTUH, bukan klik mouse sintetis.

        # Pilih tombol + yang BENAR-BENAR visible (layout lebar pakai
        # "Buat" expanded; svg "Postingan baru" hanya ada di layout sempit
        # dan tersembunyi - .first gabungan bisa mengenai yang hidden).
        item = None
        for lbl in _PLUS_LABELS:
            loc = page.locator(f'[aria-label="{lbl}"]')
            if not await loc.count():
                continue
            cand = loc.first
            try:
                await cand.wait_for(state="visible", timeout=2_500)
                item = cand
                break
            except Exception:  # noqa: BLE001 - coba label berikutnya
                continue
        if item is None:
            raise PlatformError(
                "Tombol Buat (+) tidak ditemukan/terlihat di halaman IG."
            )
        target = item.locator('xpath=ancestor::*[@role="button" or self::a][1]')
        if await target.count():
            item = target.first
        plus_box = await item.bounding_box()
        if plus_box is None:
            raise PlatformError(
                "Tombol Buat (+) Instagram tidak punya posisi di halaman; "
                "tata letaknya mungkin berubah."
            )
        await _tap(page, item, "tombol Buat (+)")
        await page.wait_for_timeout(2_500)

        # 1b. Tombol + punya DUA perilaku, tergantung versi tampilan yang
        #     dilayani ke akun: ada yang LANGSUNG membuka komposer "Buat
        #     postingan baru", ada yang memunculkan popover pilihan jenis
        #     konten dulu. Komposer diperiksa LEBIH DAHULU, sebab kalau ia
        #     sudah terbuka, mengklik + sekali lagi justru MENUTUPNYA - itulah
        #     yang dulu membuat tiap percobaan ulang berakhir gagal.
        #     Pemindaian popover dibatasi ke region di bawah tombol + (kolom
        #     sidebar) supaya kata "Postingan" di caption feed tidak terbaca.
        pindai_js = """(plus) => {
            const cands = [];
            for (const e of document.querySelectorAll('*')) {
                const t = (e.innerText || '').trim();
                if (t !== 'Postingan' && t !== 'Post') continue;
                const r = e.getBoundingClientRect();
                if (r.height < 30 || r.width < 100) continue;
                if (getComputedStyle(e).pointerEvents === 'none') continue;
                if (r.y < plus.y + plus.height - 10) continue;
                const colPlus = plus.x + plus.width / 2;
                if (Math.abs((r.x + r.width / 2) - colPlus) > 120) continue;
                cands.push({x: r.x, y: r.y, width: r.width, height: r.height});
            }
            return cands.slice(0, 3);
        }"""

        async def komposer_terbuka() -> bool:
            """Dialog unggah sudah tampil?

            Dikenali dari adanya input berkas DI DALAM dialog, bukan dari
            kata-kata tombolnya. Teksnya berubah-ubah - pernah "Pilih dari
            komputer", sekarang "Pilih dari perangkat" - dan menebaknya dari
            teks membuat kode mengira komposer belum terbuka, lalu menap
            tombol + sekali lagi sehingga komposernya justru tertutup.
            """
            return await page.locator(
                'div[role="dialog"]:has(input[type="file"])'
            ).count() > 0

        row_box = None
        komposer = False
        for percobaan in range(4):
            if percobaan:  # ketukan pertama sudah dilakukan di atas
                try:
                    await _tap(page, item, "tombol Buat (+) (ulang)")
                except Exception:  # noqa: BLE001 - tombol bisa tertutup dialog
                    pass          # keadaannya diperiksa lagi di bawah
                await page.wait_for_timeout(2_000)
            if await komposer_terbuka():
                komposer = True
                print("[ig] komposer terbuka langsung dari tombol +", flush=True)
                break
            scan = await page.evaluate(pindai_js, plus_box)
            if scan:
                row_box = scan[0]
                break

        if not komposer and row_box is None:
            raise PlatformError(
                "Tombol Buat (+) sudah diklik tetapi komposer maupun popover "
                "'Postingan' tidak muncul. Pastikan jendela Chrome terlihat "
                "penuh dan tidak tertutup jendela lain."
            )
        if row_box is not None:
            await _tap(page, row_box, "baris Postingan")

        dialog_seen = False
        for _ in range(10):
            await page.wait_for_timeout(1_000)
            if await page.locator('div[role="dialog"]').count():
                dialog_seen = True
                break
        if not dialog_seen:
            raise PlatformError(
                "Komposer Instagram tidak terbuka setelah klik HID. "
                "Coba lagi; jika terus gagal perbarui Session ID di "
                "Account Manager."
            )
        print("[ig] komposer terbuka", flush=True)
        lapor(*tahap.KOMPOSER)

        # 2. Attach the media via the OS file chooser (Playwright intercepts
        #    it, so no native dialog shows up). The chooser trigger only
        #    exists AFTER the composer opens.
        try:
            pick = page.get_by_text(
                re.compile(r"Pilih dari (komputer|perangkat)|Select from (computer|device)"),
            ).first
            async with page.expect_file_chooser(timeout=8_000) as fc_info:
                await pick.click(timeout=5_000)
            chooser = await fc_info.value
            await chooser.set_files(str(media_path.resolve()), timeout=120_000)
            print("[ig] file attached via file chooser", flush=True)
        except Exception as exc:  # noqa: BLE001 - fallback ke input langsung
            print(f"[ig] file chooser gagal ({exc}); pakai input[type=file]",
                  flush=True)
            file_input = page.locator(
                'div[role="dialog"] input[type="file"], input[type="file"]'
            ).first
            await file_input.set_input_files(
                str(media_path.resolve()), timeout=60_000)
            print("[ig] file attached (input langsung)", flush=True)
        await page.wait_for_timeout(4_000)
        lapor(*tahap.LAMPIR)

        # Some flows (esp. video) open the composer/editor in a NEW TAB -
        # follow it, otherwise all further selectors would run on the
        # empty /create/ landing page.
        for extra in page.context.pages:
            if extra is not page and "instagram.com" in extra.url:
                page = extra
                try:
                    await page.bring_to_front()
                except Exception:  # noqa: BLE001 - bring_to_front is best-effort
                    pass
                print(f"[ig] switched to tab: {page.url}", flush=True)
                break

        # 3-5. Walk the composer wizard until Share becomes clickable.
        # The number of screens varies (crop -> filters -> tag people ->
        # add details) and differs for video vs photo, so a state machine
        # handles any sequence: Share if present, else Next, else caption.
        # Semua selektor DI-SCOPE ke dalam dialog komposer: bisa ada >1
        # dialog terbuka, dan tombol di dialog lain tidak relevan.
        # Dialog mana yang dianggap komposer. Daftarnya HARUS memuat kedua
        # bahasa: Instagram memberi antarmuka Inggris di server dan Indonesia
        # di laptop. Tanpa "Next" di sini, layar Crop berbahasa Inggris - yang
        # hanya punya tombol "Crop" dan "Next" - tidak cocok dengan selektor
        # mana pun, sehingga seluruh klik sesudahnya menyasar elemen yang
        # tidak ada dan prosesnya mandek di situ sampai batas waktu.
        _LABEL_KOMPOSER = ("Next", "Selanjutnya", "Berikutnya", "Share", "Bagikan")
        composer_sel = ", ".join(
            f'div[role="dialog"]:has([role="button"]:has-text("{x}"))'
            for x in _LABEL_KOMPOSER
        ) + ', div[role="dialog"]:has(textarea), div[role="dialog"]:has([role="textbox"])'
        composer = page.locator(composer_sel).first
        caption_done = False
        shared = False
        crop_done = False
        # Up to ~6 minutes: big videos need time to process inside the
        # composer before Share becomes clickable.
        for i in range(40):
            # Dialog pemberitahuan bisa muncul KAPAN SAJA di tengah wizard,
            # bukan hanya sesudah menekan Selanjutnya, dan selama ia terbuka
            # semua tombol di bawahnya tidak bisa diklik.
            await dismiss_dialogs()
            if i % 5 == 4:
                try:
                    dbg = await page.evaluate(
                        """() => [...document.querySelectorAll('[role="dialog"]')]
                            .map(d => (d.innerText || '').slice(0, 90))""")
                    print(f"[ig] dialogs: {dbg}", flush=True)
                except Exception:  # noqa: BLE001
                    pass
            # Layar caption muncul BERSAMA tombol Bagikan: isi caption lebih
            # dulu supaya posting tidak terkirim tanpa keterangan.
            if not caption_done:
                try:
                    caption_box = composer.locator(
                        'textarea, [role="textbox"][contenteditable="true"]'
                    ).first
                    await caption_box.click(timeout=3_000)
                    await caption_box.fill(caption[:2200])
                    caption_done = True
                    print("[ig] caption terisi", flush=True)
                    lapor(*tahap.CAPTION)
                except Exception:  # noqa: BLE001 - caption screen may not be up yet
                    pass

            share_btn = composer.locator(
                '[role="button"]:has-text("Share"), '
                '[role="button"]:has-text("Bagikan")'
            ).first
            try:
                await share_btn.click(timeout=2_500)
                lapor(*tahap.KIRIM)
                shared = True
                break
            except Exception:  # noqa: BLE001 - no Share button yet
                pass

            # Crop screen (video): pilih rasio ASLI dulu sebelum lanjut,
            # supaya video tidak dipotong ke rasio bawaan IG.
            if not crop_done and media_kind is MediaKind.VIDEO:
                crop_done = True
                try:
                    crop_toggle = composer.locator(
                        '[role="button"]:has-text("Pangkas"), '
                        'button:has-text("Pangkas"), '
                        '[role="button"]:has-text("Crop"), '
                        'button:has-text("Crop"), [role="combobox"]'
                    ).first
                    await crop_toggle.click(timeout=2_000)
                    await page.wait_for_timeout(800)
                    print("[ig] buka kontrol rasio Pangkas", flush=True)
                except Exception:  # noqa: BLE001 - kontrol rasio opsional
                    pass
                for opt_label in ("Asli", "Original"):
                    try:
                        opt = composer.locator(
                            f'[role="option"]:has-text("{opt_label}"), '
                            f'[role="button"]:has-text("{opt_label}"), '
                            f'[role="menuitem"]:has-text("{opt_label}"), '
                            f'li:has-text("{opt_label}")'
                        ).first
                        await opt.click(timeout=2_000)
                        print(f"[ig] rasio {opt_label} dipilih", flush=True)
                        await page.wait_for_timeout(600)
                        break
                    except Exception:  # noqa: BLE001 - coba label berikutnya
                        continue

            # Advance to the next wizard screen if there is one.
            advanced = False
            for label in ("Next", "Berikutnya", "Selanjutnya"):
                try:
                    btn = composer.locator(
                        f'[role="button"]:has-text("{label}"), '
                        f'button:has-text("{label}")'
                    ).first
                    await btn.click(timeout=2_500)
                    advanced = True
                    await page.wait_for_timeout(2_000)
                    break
                except Exception:  # noqa: BLE001 - try next label
                    continue
            if advanced:
                await dismiss_dialogs()
                continue

            await page.wait_for_timeout(1_500)
        if not shared:
            raise PlatformError("Tombol Share tidak ditemukan - posting tidak terkirim.")

        # 6. Wait for publish: dialog closes or confirmation text appears.
        try:
            await page.wait_for_selector(
                'text=/Your post has been shared|Postingan telah dibagikan|has been shared/i',
                timeout=45_000,
            )
        except Exception:  # noqa: BLE001 - confirmation text varies; fall back to dialog-gone
            try:
                await page.wait_for_selector('div[role="dialog"]', state="detached", timeout=20_000)
            except Exception:  # noqa: BLE001
                await page.wait_for_timeout(8_000)
        await dismiss_dialogs()
        lapor(*tahap.TAUTAN)
        if url_profil:
            tautan = await tautan_baru(page, url_profil, _PEMILIH_POST, sebelum)
            if tautan:
                print(f"[ig] tautan: {tautan}", flush=True)
                return tautan
        return url_profil or "https://www.instagram.com/"
