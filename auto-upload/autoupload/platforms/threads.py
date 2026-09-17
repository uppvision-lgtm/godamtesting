"""Threads uploader via browser session (threads.net).

No API key needed. Authentication options:
1. Browser login via Account Manager (persistent profile), or
2. "Session ID" shortcut: paste the `sessionid` cookie value from
   DevTools -> Application -> Cookies -> threads.net.

Note: the sessionid value is reset whenever you log out / log in again on the
web, and Threads rotates it periodically - refresh the Session ID when that
happens (roughly every few weeks).
"""

from pathlib import Path

from autoupload.browser import MediaKind, PlaywrightPlatform


class Threads(PlaywrightPlatform):
    platform_key = "threads"
    start_url = "https://www.threads.net"
    # Threads pindah dari threads.net ke threads.com; alamat lama masih
    # mengalihkan ke sana. Cookie harus dipasang di KEDUA domain, kalau tidak
    # sesinya hilang begitu pengalihan terjadi dan ia tampak seperti mati.
    session_cookie_map = {"sessionid": [".threads.net", ".threads.com"]}

    async def _do_upload(
        self, page, media_path: Path, caption: str, media_kind: MediaKind, **options
    ) -> str | None:
        await page.goto("https://www.threads.net/", timeout=60_000)
        await page.wait_for_timeout(3000)

        # Invalid session -> login page / no composer.
        content = await page.content()
        if "login" in page.url.lower():
            raise RuntimeError(
                "Sesi Threads tidak valid (belum login). Login ulang lewat Account "
                "Manager atau perbarui Session ID."
            )

        # Open the composer (Post button in the sidebar, or the compose icon).
        opened = False
        for sel in [
            "div[role=button]:has-text('Post')",
            "button:has-text('Post')",
            "[aria-label='Post']",
            "[aria-label='Create']",
        ]:
            loc = page.locator(sel).first
            try:
                await loc.wait_for(state="visible", timeout=4_000)
                await loc.click()
                opened = True
                break
            except Exception:  # noqa: BLE001
                continue
        if not opened:
            await page.goto("https://www.threads.net/intent/post?text=x", timeout=30_000)
        await page.wait_for_timeout(2000)

        # Type the caption into the contenteditable composer.
        composer = page.locator(
            "div[contenteditable='true']"
        ).first
        await composer.wait_for(state="visible", timeout=30_000)
        await composer.click()
        await composer.type(caption[:500], delay=15)

        # Attach media (photo or video both use the same file input).
        file_input = page.locator("input[type=file]").first
        try:
            await file_input.wait_for(state="attached", timeout=8_000)
            await file_input.set_input_files(str(media_path))
            await page.wait_for_timeout(4000)  # let it process the attachment
        except Exception:  # noqa: BLE001 - text-only post fallback
            if media_kind == MediaKind.VIDEO:
                raise RuntimeError("Input file Threads tidak ditemukan untuk video")

        # Submit.
        submitted = False
        for sel in ["div[role=button]:has-text('Post')", "button:has-text('Post')"]:
            loc = page.locator(sel).last
            try:
                await loc.wait_for(state="visible", timeout=6_000)
                await loc.click()
                submitted = True
                break
            except Exception:  # noqa: BLE001
                continue
        if not submitted:
            await page.keyboard.press("Enter")
        await page.wait_for_timeout(4000)
        return None
