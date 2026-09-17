"""Instagram uploader via browser session (web create-post flow).

CATATAN PENTING (terbukti lewat pengujian nyata, 2026-09-16):
IG melakukan "trust check" pada TOMBOL ENTRY saja - tombol "Buat" (+) di
sidebar dan baris "Postingan" pada popover-nya. Klik yang dikirim lewat
CDP/Playwright DIABAIKAN diam-diam (tanpa error, tanpa request jaringan),
sementara klik level OS (HID/CGEvent, mis. pyautogui) diterima normal.
Setelah komposer terbuka, semua langkah berikutnya (file chooser, Pangkas,
Edit, caption, Bagikan) bisa dikendalikan lewat Playwright biasa.
Karena itu upload Instagram WAJIB headful (jendela Chrome terlihat).
"""

import asyncio
import os  # noqa: S404 - baca env IG_OFFSCREEN
import subprocess  # noqa: S404 - osascript untuk membawa Chrome ke depan
from pathlib import Path

from autoupload.browser import MediaKind, PlatformError, PlaywrightPlatform

try:  # pyautogui mengirim event HID nyata (CGEventPost) di macOS
    import pyautogui
except Exception:  # noqa: BLE001 - HEADLESS/severe env tanpa display
    pyautogui = None

# Label tombol entry + baris popover, EN + ID
_PLUS_LABELS = ("Buat", "Create", "New post", "Postingan baru")


async def _hid_click(page, box: dict, label: str) -> None:
    """Klik level-OS pada koordinat layar yang dihitung dari bounding box."""
    if pyautogui is None:
        raise PlatformError(
            "pyautogui tidak tersedia; upload Instagram butuh mode headful "
            "di desktop macOS dengan pyautogui terpasang."
        )
    geo = await page.evaluate(
        "({sx: window.screenX, sy: window.screenY, oh: window.outerHeight,"
        " ih: window.innerHeight})"
    )
    if geo["oh"] <= geo["ih"]:
        raise PlatformError(
            "Viewport IG teremulasi (bukan jendela nyata) - pemetaan "
            "koordinat klik HID tidak valid. Launch IG tanpa viewport."
        )
    sx = geo["sx"] + box["x"] + box["width"] / 2
    sy = geo["sy"] + (geo["oh"] - geo["ih"]) + box["y"] + box["height"] / 2
    # Pastikan jendela Chrome fokus sebelum mengirim klik HID.
    for _ in range(5):
        if await page.evaluate("document.hasFocus()"):
            break
        subprocess.run(
            ["osascript", "-e", 'tell application "Google Chrome" to activate'],
            check=False, capture_output=True,
        )
        await page.wait_for_timeout(1_000)
    else:
        raise PlatformError(
            "Jendela Chrome tidak fokus - klik HID akan mendarat di aplikasi "
            "lain. Jangan minimalkan Chrome saat upload IG berjalan."
        )
    cx = sx - geo["sx"]
    cy = sy - geo["sy"] - (geo["oh"] - geo["ih"])
    await asyncio.to_thread(pyautogui.moveTo, sx, sy, 0.25)
    await page.wait_for_timeout(300)
    under = await page.evaluate(
        """([cx, cy]) => {
            const e = document.elementFromPoint(cx, cy);
            return e ? (e.tagName + '|' + (e.innerText || '').trim().slice(0, 30))
                     : null;
        }""",
        [cx, cy],
    )
    print(f"[ig] HID {label} di ({sx:.0f},{sy:.0f}); elemen di bawah kursor: {under}",
          flush=True)
    await asyncio.to_thread(pyautogui.click, sx, sy)


class Instagram(PlaywrightPlatform):
    platform_key = "instagram"
    # Enter via the home feed and click the nav "Create" (+) button: the
    # direct /create/ route no longer opens the composer for all accounts
    # (it can render a profile page instead).
    start_url = "https://www.instagram.com/"
    # Session ID shortcut: the Instagram "sessionid" browser cookie.
    session_cookie_map = {"sessionid": ".instagram.com"}
    # Jendela NYATA tanpa emulasi viewport: koordinat klik HID dihitung dari
    # koordinat CSS halaman, dan emulasi viewport Playwright merusak
    # pemetaan tersebut (outerHeight < innerHeight -> klik mendarat melenceng).
    viewport_override = None
    # Posisi jendela deterministik agar klik HID selalu jatuh di layar.
    extra_args = ["--window-position=0,0"]

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        # Tombol entry IG hanya merespons klik HID nyata -> wajib headful.
        self.headless = False
        # Mode latar belakang (IG_OFFSCREEN=1): jendela nyata dipindah KELUAR
        # layar sehingga upload tidak mengganggu pengguna. Klik HID tetap
        # valid karena koordinat layar dihitung dari window.screenX/screenY
        # jendela itu sendiri (nilai screenX negatif -> klik di koordinat
        # global negatif, tetap diterima window server macOS). Jendela tetap
        # harus headful: IG menolak klik CDP pada tombol entry, jadi headless
        # murni tidak mungkin.
        if os.environ.get("IG_OFFSCREEN", "").strip().lower() in ("1", "true", "yes"):
            self.extra_args = ["--window-position=-3200,0"]


    async def _do_upload(
        self, page, media_path: Path, caption: str, media_kind: MediaKind, **options: object
    ) -> str | None:
        await page.goto(self.start_url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(4_000)

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
            for label in (
                "Not now", "Nanti", "Bukan sekarang", "Lain Kali",
                "Turn On", "Nyalakan", "Selesai", "Done",
            ):
                try:
                    await page.get_by_role("button", name=label).first.click(timeout=1_500)
                    await page.wait_for_timeout(600)
                except Exception:  # noqa: BLE001 - dialog optional
                    pass

        await dismiss_dialogs()

        # 1. Open the composer via the nav "Create" (+). The nav renders
        #    late, so WAIT for it. IG trust-checks the entry buttons, so the
        #    clicks MUST be real OS-level (HID) events, not CDP clicks.
        subprocess.run(
            ["osascript", "-e", 'tell application "Google Chrome" to activate'],
            check=False, capture_output=True,
        )
        await page.bring_to_front()
        await page.wait_for_timeout(800)

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
                "Tombol Buat (+) Instagram tidak terlihat di layar - "
                "pastikan jendela Chrome tampil di monitor."
            )
        await _hid_click(page, plus_box, "tombol Buat (+)")
        await page.wait_for_timeout(2_500)

        # 1b. "+" membuka popover pilihan jenis konten; klik baris
        #     "Postingan"-nya (juga di-trust-check). Pemindaian DIBATASI ke
        #     region di bawah tombol + (kolom sidebar) supaya tidak salah
        #     membaca kata "Postingan" di caption/feed di luar konteks.
        #     Baris popover asli: tinggi >= 30px, pointer-events aktif.
        row_box = None
        scan = await page.evaluate(
            """(plus) => {
                const cands = [];
                for (const e of document.querySelectorAll('*')) {
                    const t = (e.innerText || '').trim();
                    if (t !== 'Postingan' && t !== 'Post') continue;
                    const r = e.getBoundingClientRect();
                    if (r.height < 30 || r.width < 100) continue;
                    if (getComputedStyle(e).pointerEvents === 'none') continue;
                    // harus di BAWAH tombol + dan dekat kolomnya
                    if (r.y < plus.y + plus.height - 10) continue;
                    const colPlus = plus.x + plus.width / 2;
                    if (Math.abs((r.x + r.width / 2) - colPlus) > 120) continue;
                    cands.push({x: r.x, y: r.y, width: r.width, height: r.height});
                }
                return cands.slice(0, 3);
            }""",
            plus_box,
        )
        for _ in range(3):
            if scan:
                row_box = scan[0]
                break
            await _hid_click(page, plus_box, "tombol Buat (+) (ulang)")
            await page.wait_for_timeout(2_000)
            scan = await page.evaluate(
                """(plus) => {
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
                }""",
                plus_box,
            )
        if row_box is None:
            raise PlatformError(
                "Popover 'Postingan' tidak muncul setelah klik tombol Buat. "
                "Pastikan jendela Chrome tidak tertutup jendela lain."
            )
        await _hid_click(page, row_box, "baris Postingan")

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

        # 2. Attach the media via the OS file chooser (Playwright intercepts
        #    it, so no native dialog shows up). The chooser trigger only
        #    exists AFTER the composer opens.
        try:
            pick = page.get_by_text("Pilih dari komputer", exact=False).first
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
        composer_sel = 'div[role="dialog"]:has([role="button"]:has-text(' \
            '"Selanjutnya")), div[role="dialog"]:has([role="button"]:has-text(' \
            '"Berikutnya")), div[role="dialog"]:has([role="button"]:has-text(' \
            '"Bagikan")), div[role="dialog"]:has([role="button"]:has-text(' \
            '"Share")), div[role="dialog"]:has(textarea), ' \
            'div[role="dialog"]:has([role="textbox"])'
        composer = page.locator(composer_sel).first
        caption_done = False
        shared = False
        crop_done = False
        # Up to ~6 minutes: big videos need time to process inside the
        # composer before Share becomes clickable.
        for i in range(40):
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
                except Exception:  # noqa: BLE001 - caption screen may not be up yet
                    pass

            share_btn = composer.locator(
                '[role="button"]:has-text("Share"), '
                '[role="button"]:has-text("Bagikan")'
            ).first
            try:
                await share_btn.click(timeout=2_500)
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
                        'button:has-text("Pangkas"), [role="combobox"]'
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
        return "https://www.instagram.com/"
