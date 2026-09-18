"""Facebook personal-profile uploader via Playwright (API does not allow profiles)."""

from pathlib import Path

from autoupload.browser import MediaKind, PlatformError, PlaywrightPlatform


class Facebook(PlaywrightPlatform):
    """Posts to a personal Facebook profile through the web composer.

    NOTE: selectors may need maintenance when Facebook changes its UI.
    """

    platform_key = "facebook"
    start_url = "https://www.facebook.com/"
    # Session ID shortcut: Facebook needs TWO cookies - c_user (your numeric
    # user id) and xs (the session token). Paste as "<c_user>|<xs>".
    session_cookie_map = {"c_user": ".facebook.com", "xs": ".facebook.com"}

    async def _do_upload(self, page, media_path: Path, caption: str, media_kind: MediaKind, **options) -> str | None:
        # Platform ini belum punya penjadwalan di sini. Ditolak supaya
        # tidak diam-diam terbit SEKARANG padahal diminta nanti.
        if options.get("jadwal"):
            raise PlatformError(
                "Penjadwalan Facebook belum didukung di sini. Hapus jadwalnya untuk mengirim sekarang, atau keluarkan Facebook dari platform tujuan."
            )

        await page.goto(self.start_url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(2500)

        # Dismiss popups (notification prompts, etc.) that overlay the page and
        # swallow clicks on the composer / Post button.
        for label in ("Nanti", "Not now", "OK", "Tutup", "Close"):
            try:
                await page.locator(
                    f'div[role="button"][aria-label="{label}"], '
                    f'div[role="dialog"] div[aria-label="{label}"]'
                ).first.click(timeout=1_500)
                await page.wait_for_timeout(800)
            except Exception:  # noqa: BLE001 - popup optional
                pass

        # Clear error when the session is dead (login page shown) - otherwise
        # the composer selector below times out with a confusing message.
        login_form = await page.locator(
            'input[name="password"], #loginform, form[action*="login" i]'
        ).count()
        if login_form or "login" in page.url.lower():
            raise RuntimeError(
                "Sesi Facebook tidak valid (ter-logout). Perbarui Session ID "
                "(c_user|xs) di Account Manager atau login ulang lewat Login Browser."
            )

        # Open the "Photo/Video" composer dialog. Label depends on the
        # account's language (Indonesian "Foto/video", English "Photo/video").
        compose = page.locator(
            'div[role="button"]:has-text("Foto/video"), '
            'div[role="button"]:has-text("Photo/video"), '
            'div[aria-label*="Foto/video" i], div[aria-label*="Photo/video" i]'
        ).first
        await compose.click(timeout=20_000)
        await page.wait_for_timeout(2000)

        # Fill the caption (optional - some dialog variants expose it late).
        try:
            caption_box = page.locator('div[role="textbox"][contenteditable="true"]').first
            await caption_box.click(timeout=15_000)
            await page.keyboard.type(caption[:5000], delay=10)
        except Exception:  # noqa: BLE001 - caption box may not be focusable yet
            pass

        # Attach the media (must succeed - the post is pointless without it).
        file_input = page.locator('input[type="file"]').first
        await file_input.set_input_files(str(media_path), timeout=30_000)
        await page.wait_for_timeout(5000)  # wait for upload preview

        # Click Post (label varies by language AND dialog variant: the
        # photo/video composer uses "Kirim" in Indonesian, text posts use
        # "Posting"). Must be a *button*, otherwise the composer dialog
        # itself (aria-label="Buat postingan") matches and the click fails.
        post_btn = page.locator(
            'div[role="button"][aria-label="Kirim"]:visible, '
            'div[role="button"][aria-label="Send"]:visible, '
            'div[role="button"][aria-label="Posting"]:visible, '
            'div[role="button"][aria-label="Post"]:visible, '
            'div[role="button"][aria-label*="Kirim" i]:visible, '
            'div[role="button"][aria-label*="Posting" i]:visible, '
            'div[role="button"][aria-label*="Post" i]:visible'
        ).first
        for _ in range(4):
            try:
                await post_btn.click(timeout=15_000)
                break
            except Exception:  # noqa: BLE001 - media may still be uploading
                await page.wait_for_timeout(5_000)
        await page.wait_for_timeout(4000)
        return None
