"""YouTube uploader via browser session (studio.youtube.com).

No API key needed. Two ways to authenticate:
1. Browser login via Account Manager (persistent profile), or
2. "Session ID" shortcut: paste the Google/YouTube cookie values joined by "|"
   in exactly this order (from DevTools -> Application -> Cookies -> youtube.com).
   Minimal (7, inti): SID | HSID | SSID | APISID | SAPISID |
                       __Secure-1PSID | __Secure-3PSID
   Lengkap (10): 7 di atas + __Secure-1PSIDTS | __Secure-3PSIDTS | LOGIN_INFO
   (PSIDTS berumur pendek dan di-refresh Google sendiri; LOGIN_INFO opsional.)

Note: cookies reset whenever you log out / log in again on that browser, and
Google rotates them periodically - refresh the Session ID when that happens.
"""

import asyncio
import re
from pathlib import Path

from autoupload.browser import MediaKind, PlaywrightPlatform


class YouTube(PlaywrightPlatform):
    platform_key = "youtube"
    start_url = "https://studio.youtube.com"
    session_cookie_map = {
        "SID": [".google.com", ".youtube.com"],
        "HSID": [".google.com", ".youtube.com"],
        "SSID": [".google.com", ".youtube.com"],
        "APISID": [".google.com", ".youtube.com"],
        "SAPISID": [".google.com", ".youtube.com"],
        "__Secure-1PSID": [".google.com", ".youtube.com"],
        "__Secure-3PSID": [".google.com", ".youtube.com"],
        "__Secure-1PSIDTS": [".google.com", ".youtube.com"],
        "__Secure-3PSIDTS": [".youtube.com", ".google.com"],
        "LOGIN_INFO": [".youtube.com", ".google.com"],
    }
    # Short-lived timestamps Google refreshes client-side + persistence helper:
    # boleh diabaikan user (cukup tempel 7 cookie inti).
    session_cookie_optional = ("__Secure-1PSIDTS", "__Secure-3PSIDTS", "LOGIN_INFO")

    async def _dismiss_browser_banner(self, page) -> bool:
        """Dismiss Studio's "unsupported browser" full-page interstitial.

        Automated (headless) Chrome gets flagged by studio.youtube.com and the
        entire UI is replaced by a "SKIP TO YOUTUBE STUDIO" splash screen.
        Click it so the real Studio (and its file input) becomes reachable.
        Returns True if a banner was found and skipped.
        """
        for label in ("SKIP TO YOUTUBE STUDIO", "Lewati ke YouTube Studio"):
            loc = page.get_by_text(label, exact=False).first
            try:
                await loc.wait_for(state="visible", timeout=3_000)
                await loc.click()
                await page.wait_for_timeout(2_000)
                return True
            except Exception:  # noqa: BLE001 - no banner, try next label
                continue
        return False

    async def _open_upload_dialog(self, page) -> None:
        """Open the upload dialog via CREATE -> "Upload videos".

        Playwright role/text locators are unreliable on Studio's polymer web
        components (the header Create button is not exposed as a normal
        a11y button in headless), so the elements are clicked directly via
        JS - validated against Studio in headless Chrome.
        """
        clicked = await page.evaluate(
            """() => {
              const txt = (el) => (el.textContent || '').trim();
              const btn = [...document.querySelectorAll(
                'ytcp-button, tp-yt-paper-button, button, [role=button]')]
                .find(el => /^(create|buat)$/i.test(txt(el)));
              if (btn) { btn.click(); return true; }
              return false;
            }"""
        )
        if not clicked:  # JS path failed - try accessibility-role clicks
            for label in ("Create", "Buat", "CREATE"):
                try:
                    await page.get_by_role("button", name=label, exact=False).first.click(
                        timeout=5_000
                    )
                    break
                except Exception:  # noqa: BLE001 - try next label
                    continue
        await page.wait_for_timeout(1_500)
        opened = await page.evaluate(
            """() => {
              const items = [...document.querySelectorAll(
                'tp-yt-paper-item, [role=menuitem]')];
              const it = items.find(el =>
                /^(upload videos|upload file|unggah video)$/i
                  .test((el.textContent || '').trim()));
              if (it) { it.click(); return true; }
              return false;
            }"""
        )
        if not opened:
            for label in ("Upload videos", "Upload file", "Unggah video"):
                try:
                    await page.get_by_role("menuitem", name=label).first.click(
                        timeout=5_000
                    )
                    break
                except Exception:  # noqa: BLE001 - try next label
                    try:
                        await page.get_by_text(label, exact=False).first.click(
                            timeout=3_000
                        )
                        break
                    except Exception:  # noqa: BLE001
                        continue

    async def _click_dialog_button(
        self, page, ids: list[str], labels: list[str], timeout_s: int = 45
    ) -> bool:
        """Click an upload-dialog primary button (Next / Publish) robustly.

        Studio's dialog buttons are polymer <ytcp-button> web components that
        Playwright role locators cannot see in headless Chrome, so click via
        JS - retrying while the button is still disabled (video processing /
        checks step can keep Next disabled for a while).
        """
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            clicked = await page.evaluate(
                """([ids, labels]) => {
                  const txt = (el) => (el.textContent || '').trim().toLowerCase();
                  // Polymer/custom elements may not expose a usable .disabled
                  // property, so also check the attributes Studio sets on
                  // disabled buttons - clicking while disabled is silently
                  // ignored (which used to leave uploads as drafts).
                  const dead = (el) => el.disabled === true
                    || el.hasAttribute('disabled')
                    || el.getAttribute('aria-disabled') === 'true';
                  for (const id of ids) {
                    const el = document.querySelector(id);
                    if (el && !dead(el) && !el.hasAttribute('hidden')) {
                      el.click(); return 'id:' + id;
                    }
                  }
                  const cands = [...document.querySelectorAll(
                    'ytcp-button, tp-yt-paper-button, button')]
                    .filter(el => labels.includes(txt(el)) && !dead(el));
                  const btn = cands.find(el => el.offsetParent !== null) || cands[0];
                  if (btn) { btn.click(); return 'text:' + txt(btn); }
                  return null;
                }""",
                [ids, labels],
            )
            if clicked:
                return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await page.wait_for_timeout(2_000)

    async def _capture_video_url(self, page, timeout_s: int = 25) -> str:
        """Best-effort capture of the uploaded video's URL after finalizing.

        The confirmation panel (and later the dialog itself) exposes the
        watch/youtu.be link; poll briefly since the link is created after
        the video entry is registered.
        """
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            try:
                url = await page.evaluate(
                    r"""() => {
                      const a = [...document.querySelectorAll('a[href]')]
                        .map(el => el.href || '')
                        .find(h => h.includes('watch?v=') || h.includes('youtu.be/'));
                      if (a) return a;
                      const m = (document.body.innerHTML || '')
                        .match(/youtu\.be\/([\w-]{11})/);
                      return m ? 'https://youtu.be/' + m[1] : '';
                    }"""
                )
            except Exception:  # noqa: BLE001 - page may navigate mid-scan
                url = ""
            if url:
                return url
            if asyncio.get_running_loop().time() >= deadline:
                return ""
            await page.wait_for_timeout(3_000)

    async def _save_upload(self, page, timeout_s: int = 120) -> None:
        """Click the dialog's finalize button until Studio confirms the save.

        A single JS click can be silently ignored (button re-disabled while
        processing, or the Polymer handler not attached yet); the dialog then
        stays open and closing the browser leaves the upload as a DRAFT even
        though the video id / youtu.be link are already visible. So after
        every click, poll for a real confirmation: the dialog is gone, the
        published/saved panel is shown, or the Publish/Save button has been
        removed (2 consecutive polls). Retry until the deadline, then raise.
        """
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            await self._click_dialog_button(
                page,
                ["#done-button"],
                ["publish", "publikasikan", "save", "simpan"],
                timeout_s=5,
            )
            confirmed = False
            strikes = 0
            for _ in range(5):  # ~15s of confirmation polling per attempt
                await page.wait_for_timeout(3_000)
                state = await page.evaluate(
                    """() => {
                      const dlg = document.querySelector('ytcp-uploads-dialog');
                      const btn = document.querySelector('#done-button');
                      const dead = (el) => !el || el.disabled === true
                        || el.hasAttribute('disabled')
                        || el.getAttribute('aria-disabled') === 'true'
                        || el.offsetParent === null;
                      return {
                        dialogOpen: !!dlg && dlg.offsetParent !== null,
                        doneGone: dead(btn),
                        confirmed: /video (published|saved)|terpublikasikan|tersimpan/i
                          .test(document.body.innerText || ''),
                      };
                    }"""
                )
                if state["confirmed"] or not state["dialogOpen"]:
                    confirmed = True
                    break
                if state["doneGone"]:
                    strikes += 1
                    if strikes >= 2:
                        confirmed = True
                        break
                else:
                    strikes = 0
            if confirmed:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(
                    "Dialog upload tidak menyelesaikan Publish/Save - video "
                    "kemungkinan masih DRAFT. Coba lagi."
                )

    async def _do_upload(
        self, page, media_path: Path, caption: str, media_kind: MediaKind, **options
    ) -> str | None:
        if media_kind != MediaKind.VIDEO:
            raise RuntimeError("YouTube via browser hanya mendukung video")

        await page.goto("https://studio.youtube.com/channel/UC/videos/upload", timeout=60_000)
        # If redirected away from studio, the session is not valid.
        if "studio.youtube.com" not in page.url:
            raise RuntimeError(
                "Sesi YouTube tidak valid (belum login). Login ulang lewat Account "
                "Manager atau perbarui Session ID."
            )

        # Headless/automated Chrome (HeadlessChrome UA) makes Studio show a
        # full-page "unsupported browser" interstitial that hides the whole
        # upload flow. Dismiss it first ("SKIP TO YOUTUBE STUDIO"); afterwards
        # the upload dialog must be opened manually via Create -> Upload videos.
        banner_dismissed = await self._dismiss_browser_banner(page)

        # Upload dialog may take a moment; ensure the file input exists.
        # Newer Studio layouts hide the input behind CREATE -> "Upload videos".
        file_input = page.locator("input[type=file]").first
        try:
            await file_input.wait_for(
                state="attached", timeout=8_000 if banner_dismissed else 15_000
            )
        except Exception:  # noqa: BLE001 - open the dialog manually
            await self._open_upload_dialog(page)
            await file_input.wait_for(state="attached", timeout=20_000)
        await file_input.set_input_files(str(media_path))

        lines = [l for l in caption.splitlines() if l.strip()]
        title_text = str(options.get("title") or (lines[0] if lines else "Video"))[:95]

        # Title & description boxes are contenteditable textareas.
        title_box = page.locator("ytcp-social-suggestions-textbox #textbox").first
        await title_box.wait_for(state="visible", timeout=60_000)
        await title_box.click()
        await title_box.fill("")
        await title_box.type(title_text, delay=20)

        if len(lines) > 1:
            desc_box = page.locator("ytcp-mention-textbox #textbox").nth(1)
            try:
                await desc_box.wait_for(state="visible", timeout=10_000)
                await desc_box.click()
                await desc_box.type("\n".join(lines[1:])[:4900], delay=10)
            except Exception:  # noqa: BLE001 - description is optional
                pass

        # The dialog carries the video id from the moment the upload is
        # registered - grab it early (the final panel does not always show
        # a clickable youtu.be link).
        video_id = await page.evaluate(
            """() => {
              const d = document.querySelector('ytcp-uploads-dialog');
              return (d && d.getAttribute('video-id')) || '';
            }"""
        )

        # Answer the required "made for kids" question - leaving it unanswered
        # keeps the dialog stuck on Details (Next is accepted but never
        # advances, so Publish never becomes enabled).
        await page.evaluate(
            """() => {
              const r = document.querySelector(
                'tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]');
              if (r && !(r.checked || r.active || r.hasAttribute('checked'))) {
                r.click();
              }
            }"""
        )
        await page.wait_for_timeout(800)

        # Wait until processing finishes enough to continue, then Next x3
        # (Details -> Video elements -> Checks -> Visibility).
        for _ in range(3):
            clicked = await self._click_dialog_button(
                page, ["#next-button"], ["next", "lanjut"]
            )
            if not clicked:
                break
            await page.wait_for_timeout(1_500)

        # Visibility step: Studio defaults new uploads to PRIVATE. Switch the
        # radio when another visibility was requested, and VERIFY it actually
        # got selected (a JS click can be swallowed by the Polymer element).
        # For PUBLIC/UNLISTED the primary CTA becomes "Publish" (accepted by
        # the finalize step below alongside "Save").
        vis_radio = {
            "public": "PUBLIC",
            "publik": "PUBLIC",
            "unlisted": "UNLISTED",
            "private": "PRIVATE",
            "privat": "PRIVATE",
        }.get(str(options.get("visibility") or "private").strip().lower(), "PRIVATE")
        if vis_radio != "PRIVATE":
            deadline = asyncio.get_running_loop().time() + 30
            while True:
                selected = await page.evaluate(
                    """(name) => {
                      const r = document.querySelector(
                        `tp-yt-paper-radio-button[name="${name}"]`);
                      if (!r) return null;
                      const on = r.checked
                        || r.getAttribute('aria-checked') === 'true'
                        || r.hasAttribute('checked')
                        || r.hasAttribute('active')
                        || /iron-selected/.test(r.className || '');
                      if (!on) r.click();
                      return on;
                    }""",
                    vis_radio,
                )
                if selected:
                    break
                if selected is None or asyncio.get_running_loop().time() >= deadline:
                    raise RuntimeError(
                        f"Radio visibility {vis_radio} tidak dapat dipilih - "
                        "dialog tidak berada di langkah Visibility"
                    )
                await page.wait_for_timeout(2_000)
            await page.wait_for_timeout(500)

        # Finalize the upload. With the default PRIVATE visibility the dialog's
        # primary CTA is #done-button labeled "Save" (this publishes the video
        # to the channel as private; public/unlisted say "Publish"). Keep
        # clicking until Studio actually confirms - a swallowed click leaves
        # the upload as a draft even though the dialog already shows the link.
        await self._save_upload(page)

        # Confirmation panel / dialog carry the video link.
        url = await self._capture_video_url(page)

        # Server-truth check: make sure the upload really left the draft state
        # (the dialog exposes video-id/youtu.be even before saving happened).
        if video_id:
            draft = None
            for _ in range(2):
                await page.goto(
                    f"https://studio.youtube.com/video/{video_id}", timeout=60_000
                )
                await page.wait_for_timeout(4_000)
                draft, broken = await page.evaluate(
                    """() => {
                      const body = document.body.innerText || '';
                      return [
                        /This video is in a draft state/i.test(body),
                        body.includes('Oops, something went wrong'),
                      ];
                    }"""
                )
                if not broken:
                    break
                draft = None  # page failed to load; retry once
                await page.wait_for_timeout(5_000)
            if draft:
                raise RuntimeError(
                    "Video masih berstatus DRAFT setelah Save - upload tidak "
                    "selesai dipublikasikan di Studio."
                )
        if not url and video_id:
            return f"https://youtu.be/{video_id}"
        return url or "https://studio.youtube.com"
