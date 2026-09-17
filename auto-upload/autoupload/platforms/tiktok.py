"""TikTok uploader via browser session (tiktok.com web uploader)."""

from pathlib import Path

from autoupload.browser import MediaKind, PlaywrightPlatform


class TikTok(PlaywrightPlatform):
    platform_key = "tiktok"
    # TikTok moved the uploader to "TikTok Studio" - the old /upload URL 404s.
    start_url = "https://www.tiktok.com/tiktokstudio/upload?lang=en"
    # Session ID shortcut: users can paste their browser "sessionid" cookie.
    session_cookie_map = {"sessionid": ".tiktok.com"}

    async def _do_upload(
        self, page, media_path: Path, caption: str, media_kind: MediaKind, **options: object
    ) -> str | None:
        await page.goto(self.start_url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(4_000)

        # If we got bounced to a login page the session is dead - say so clearly.
        if "/login" in page.url or "auth" in page.url:
            raise RuntimeError(
                "Sesi TikTok tidak valid (ter-logout). Login ulang lewat Account "
                "Manager atau perbarui Session ID."
            )
        login_btn = await page.locator('button:has-text("Log in"), button:has-text("Masuk")').count()
        if login_btn:
            raise RuntimeError(
                "Sesi TikTok tidak valid (halaman login muncul). Login ulang lewat "
                "Account Manager atau perbarui Session ID."
            )

        # 1. Attach the media file
        file_input = page.locator('input[type="file"]').first
        await file_input.set_input_files(str(media_path.resolve()), timeout=30_000)

        # 2. Wait for processing (caption editor becomes enabled)
        await page.wait_for_timeout(8_000)

        # 3. Fill caption (TikTok uses a Draft.js contenteditable)
        caption_box = page.locator(
            'div[contenteditable="true"].public-DraftEditor-content, '
            'div[data-e2e="caption-editor"], '
            'div[contenteditable="true"]'
        ).first
        try:
            await caption_box.click(timeout=30_000)
            await caption_box.fill("")  # clear pre-filled text
            await page.keyboard.type(caption, delay=10)
        except Exception:  # noqa: BLE001 - caption box selector may vary
            pass

        # 4. Click Post. IMPORTANT: never use :has-text("Post") here - it
        # substring-matches sidebar items ("Postingan") and clicks the wrong
        # button. Use the stable data-e2e attribute first, then exact-name
        # role lookups as fallbacks.

        # Dismiss stacked info popups that overlay the Post button
        # ("Fitur pengeditan baru" -> Mengerti; "Aktifkan pemeriksaan
        # konten otomatis?" -> Aktifkan/Batal). They can reappear while
        # the video processes, so keep dismissing during retries too.
        async def dismiss_tiktok_popups() -> None:
            for label in ("Mengerti", "Got it", "OK", "Aktifkan", "Batal", "Nanti"):
                try:
                    await page.get_by_role("button", name=label, exact=True).first.click(
                        timeout=1_200
                    )
                    await page.wait_for_timeout(500)
                except Exception:  # noqa: BLE001 - popup optional
                    pass

        await dismiss_tiktok_popups()

        post_names = ["Post", "Posting", "Publikasikan"]
        last_error: Exception | None = None
        for _ in range(6):
            try:
                # 2025+ Studio UI renamed the CTA: post_video_button
                # (was share_btn). Wait for it to be enabled: while the
                # video is still processing the button stays disabled.
                btn = page.locator(
                    'button[data-e2e="post_video_button"]:not([disabled]), '
                    'button[data-e2e="share_btn"]:not([disabled]), '
                    'div[role="button"][data-e2e="post_video_button"]'
                ).first
                await btn.click(timeout=8_000)
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 - try role-based fallback
                last_error = exc
                for name in post_names:
                    try:
                        await page.get_by_role("button", name=name, exact=True).first.click(
                            timeout=4_000
                        )
                        last_error = None
                        break
                    except Exception:  # noqa: BLE001 - try next name
                        continue
                if last_error is None:
                    break
                # Text-based fallback: a button-like element whose whole
                # text is "Posting" (may be a div[role=button], not <button>).
                try:
                    await page.locator(
                        'button:text-is("Posting"), button:text-is("Post"), '
                        'div[role="button"]:text-is("Posting")'
                    ).first.click(timeout=4_000)
                    last_error = None
                    break
                except Exception:  # noqa: BLE001 - keep retrying
                    pass
                # Popups may (re)appear and swallow the clicks above.
                await dismiss_tiktok_popups()
                await page.wait_for_timeout(5_000)
        if last_error:
            raise RuntimeError(
                "Tombol 'Post' TikTok tidak ditemukan - pastikan kamu masih login "
                "dan halaman TikTok Studio terbuka."
            ) from last_error

        # 5. Wait for confirmation
        await page.wait_for_timeout(10_000)
        return None
