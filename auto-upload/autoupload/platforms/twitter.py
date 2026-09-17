"""X / Twitter uploader via Playwright - no paid API required."""

from pathlib import Path

from autoupload.browser import MediaKind, PlaywrightPlatform


class Twitter(PlaywrightPlatform):
    platform_key = "twitter"
    start_url = "https://x.com/compose/post"
    # Session ID shortcut: the "auth_token" cookie is enough to be logged in.
    session_cookie_map = {"auth_token": ".x.com"}

    async def _do_upload(self, page, media_path: Path, caption: str, media_kind: MediaKind, **options) -> str | None:
        await page.goto(self.start_url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_selector('div[data-testid="tweetTextarea_0"]', timeout=30_000)

        # Type the caption into the compose box.
        await page.click('div[data-testid="tweetTextarea_0"]')
        await page.keyboard.type(caption[:280], delay=15)

        # Attach the media file.
        file_input = page.locator('input[data-testid="fileInput"]').first
        await file_input.set_input_files(str(media_path))
        # Wait for the uploaded media preview to appear.
        await page.wait_for_selector('div[data-testid="attachments"]', timeout=120_000)

        await page.wait_for_timeout(1500)
        await page.click('button[data-testid="tweetButton"]')
        await page.wait_for_timeout(3000)  # let the post go through
        return None  # X does not expose the new post URL in the compose flow
