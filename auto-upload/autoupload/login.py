"""Manajer login browser yang dikendalikan dari web.

Backend membuka jendela browser headful di mesin server (kalau lokal =
layar user sendiri), user login di jendela itu, lalu klik "Selesai" di web.
Tiap akun dapat profil browser permanen di browser_profiles/<platform>/<akun>/
- cookie-nya di disk, jadi login tahan restart dan bertahan berminggu-minggu.
Beberapa akun per platform = nama profil yang berbeda.
"""

import subprocess
import sys
import threading
import time

from autoupload.browser import (
    STEALTH_ARGS,
    STEALTH_INIT_SCRIPT,
    VIEWPORT,
    profile_dir,
    session_names,
)
from autoupload.config import PROFILES_DIR

LOGIN_URLS = {
    "twitter": "https://x.com/i/flow/login",
    "facebook": "https://www.facebook.com/login",
    "instagram": "https://www.instagram.com/accounts/login",
    # QR login by default: password login on TikTok often hits "maximum
    # attempts" anti-bot checks; QR via the phone app almost always works.
    "tiktok": "https://www.tiktok.com/login/qr",
    "threads": "https://www.threads.net/login",
    "youtube": (
        # The old direct accounts.google.com/v3/signin URL now returns HTTP 400
        # from Google. Use the canonical youtube.com sign-in chain instead -
        # it redirects to the proper Google login with continue=Studio.
        "https://www.youtube.com/signin?action_handle_signin=true&app=desktop"
        "&next=https%3A%2F%2Fstudio.youtube.com%2F&feature=redirect_login"
    ),
}

# Cookie that only exists after a REAL login - used to verify the session
# before saving it, so a failed login is never stored as a "valid" session.
LOGIN_COOKIE = {
    "twitter": ("auth_token",),
    "facebook": ("c_user", "xs"),
    "instagram": ("sessionid", "ds_user_id"),
    "tiktok": ("sessionid", "sessionid_ss"),
    "threads": ("sessionid",),
    "youtube": ("SID", "SAPISID"),
}

SUPPORTED = list(LOGIN_URLS)

# platform -> {running, message, saved_at, session_name}
_state: dict[str, dict] = {}
_lock = threading.Lock()
# key -> threading.Event used to signal "user finished logging in"
_finish_events: dict[str, threading.Event] = {}


def status(platform: str) -> dict:
    st = _state.get(platform, {})
    return {
        "platform": platform,
        "running": st.get("running", False),
        "message": st.get("message", ""),
        "has_session": bool(session_names(platform)),
        "saved_at": st.get("saved_at"),
        "session_name": st.get("session_name", "default"),
        "sessions": session_names(platform),
    }


def list_all() -> list[dict]:
    return [status(p) for p in SUPPORTED]


def delete_session(platform: str, session_name: str) -> dict:
    """Remove a saved profile (e.g. logged-out or invalid session)."""
    import shutil

    target = PROFILES_DIR / platform / session_name
    removed = target.exists()
    if removed:
        shutil.rmtree(target, ignore_errors=True)
    return {"ok": removed, "message": "Sesi dihapus" if removed else "Sesi tidak ditemukan"}


def _has_live_browser(platform: str) -> bool:
    """True jika ada proses Chrome hidup yang memakai profil platform ini."""
    proc = subprocess.run(
        ["pgrep", "-f", f"browser_profiles/{platform}"], capture_output=True
    )
    return proc.returncode == 0


def cancel(platform: str) -> dict:
    """Batalkan login yang nyangkut: tutup browser & reset state."""
    subprocess.run(["pkill", "-f", f"browser_profiles/{platform}"], capture_output=True)
    with _lock:
        _state[platform] = {
            "running": False,
            "message": "Login dibatalkan. Klik platform lagi untuk mencoba ulang.",
            "saved_at": None,
            "session_name": "default",
        }
    ev = _finish_events.get(platform)
    if ev:
        ev.set()
    return {"ok": True, "message": _state[platform]["message"]}


def start(platform: str, session_name: str = "default") -> dict:
    if platform not in LOGIN_URLS:
        return {"ok": False, "error": f"Browser login tidak tersedia untuk '{platform}'"}
    session_name = (session_name or "default").strip().lower().replace(" ", "_") or "default"
    with _lock:
        # Per-platform lock: one login per platform, but different platforms
        # can log in at the same time.
        if _state.get(platform, {}).get("running"):
            # Auto-recovery: if the state says "running" but the browser process
            # is actually dead (crashed/killed), reset the stale state instead
            # of blocking the user forever.
            if _has_live_browser(platform):
                return {"ok": False, "error": f"Login '{platform}' masih berjalan. Selesaikan dulu."}
            _state[platform] = {
                "running": False,
                "message": "Sesi login lama dipulihkan (browser sudah mati).",
                "saved_at": None,
                "session_name": session_name,
            }
        elif _has_live_browser(platform):
            # Orphan browser window from before a backend restart: kill it so
            # the fresh login doesn't fight over the same profile lock.
            subprocess.run(
                ["pkill", "-f", f"browser_profiles/{platform}"], capture_output=True
            )
            time.sleep(1.5)
        _state[platform] = {
            "running": True,
            "message": "Browser dibuka...",
            "saved_at": None,
            "session_name": session_name,
        }
    thread = threading.Thread(target=_run_login, args=(platform, session_name), daemon=True)
    thread.start()
    return {"ok": True}


def finish(platform: str) -> dict:
    event = _finish_events.get(platform)
    if not event:
        return {"ok": False, "error": "Tidak ada login yang berjalan"}
    event.set()  # signal the login thread to save & close
    for _ in range(60):  # wait up to 30s for the thread to persist
        time.sleep(0.5)
        if not _state.get(platform, {}).get("running"):
            break
    st = _state.get(platform, {})
    return {"ok": not st.get("running", False), "message": st.get("message", "")}


def _run_login(platform: str, session_name: str) -> None:
    from playwright.sync_api import sync_playwright

    event = threading.Event()
    _finish_events[platform] = event

    def _set(msg: str) -> None:
        with _lock:
            _state[platform] = {**_state.get(platform, {}), "running": True, "message": msg}

    _set("Jendela browser terbuka — silakan login di jendela tersebut.")
    try:
        user_data_dir = profile_dir(platform, session_name)
        user_data_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            # Real Chrome + stealth flags -> TikTok/IG are far less likely to
            # show "maximum attempts reached" bot detection.
            try:
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(user_data_dir),
                    headless=False,
                    channel="chrome",
                    args=STEALTH_ARGS,
                    viewport=VIEWPORT,
                )
            except Exception:  # noqa: BLE001 - real Chrome not installed
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(user_data_dir),
                    headless=False,
                    args=STEALTH_ARGS,
                    viewport=VIEWPORT,
                )
            context.add_init_script(STEALTH_INIT_SCRIPT)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(LOGIN_URLS[platform], timeout=60_000)

            # macOS: bring the browser window to the FRONT so the user sees it
            # immediately instead of thinking "nothing happened".
            if sys.platform == "darwin":
                for app in ("Google Chrome", "Chromium"):
                    proc = subprocess.run(
                        ["osascript", "-e", f'tell application "{app}" to activate'],
                        capture_output=True,
                    )
                    if proc.returncode == 0:
                        break

            def _aborted() -> bool:
                """User closed the login window before finishing.

                Checked two ways: Playwright's page state AND whether any real
                Chrome process with this profile dir is still alive (the latter
                catches process kills that Playwright reports late).
                """
                try:
                    if page.is_closed():
                        return True
                except Exception:  # noqa: BLE001
                    return True
                proc = subprocess.run(
                    ["pgrep", "-f", str(user_data_dir)], capture_output=True
                )
                return proc.returncode != 0

            _set("Menunggu kamu selesai login di jendela browser...")
            required_cookies = LOGIN_COOKIE[platform]
            while True:
                # Wait for the finish click OR the user closing the window.
                while not event.wait(timeout=0.5):
                    if _aborted():
                        with _lock:
                            _state[platform] = {
                                "running": False,
                                "message": "Jendela ditutup sebelum login selesai. Klik login lagi untuk mencoba ulang.",
                                "saved_at": None,
                                "session_name": session_name,
                            }
                        context.close()
                        return

                # Verify the login really succeeded before accepting it.
                cookies = {c["name"]: c for c in context.cookies()}
                logged_in = any(
                    name in cookies and cookies[name].get("value")
                    for name in required_cookies
                )
                if logged_in:
                    break
                # Not logged in yet - tell the user and keep the window open.
                _set(
                    "Login belum selesai (sesi tidak terdeteksi). "
                    "Silakan login di jendela browser, lalu klik 'Selesai' lagi."
                )
                event.clear()

            # Persistent profile keeps cookies automatically; just close.
            context.close()
            _set("Sesi tersimpan ✓ (akan tetap login untuk upload berikutnya)")
            with _lock:
                _state[platform]["running"] = False
                _state[platform]["saved_at"] = str(PROFILES_DIR / platform / session_name)
    except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
        with _lock:
            _state[platform] = {
                "running": False,
                "message": f"Gagal: {type(exc).__name__}: {exc}",
                "saved_at": None,
                "session_name": session_name,
            }

