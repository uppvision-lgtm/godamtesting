#!/usr/bin/env python3
"""Unggah satu postingan Instagram dari berkas di folder, lewat Chrome.

Cara kerjanya persis seperti orang mengunggah manual: Chrome dibuka dengan
Session ID akunmu, tombol Buat (+) di sidebar diklik, baris "Postingan"
dipilih, file dilampirkan, caption diisi, lalu tombol Bagikan ditekan.

    python3 upload_ig.py                       # ambil dari folder berkas/
    python3 upload_ig.py --video klip.mp4 --caption "halo"
    python3 upload_ig.py --folder /path/lain   # folder lain
    python3 upload_ig.py --sisakan             # jangan pindahkan ke terkirim/

ISI FOLDER berkas/
    Taruh satu file video/foto di situ, lalu tulis captionnya di file .txt.
    Caption dicari berurutan: <nama-video>.txt, lalu caption.txt.
    Contoh: berkas/klip.mp4 + berkas/klip.txt

HARUS DIJALANKAN DARI Terminal.app, BUKAN dari terminal VS Code.
    Instagram diam-diam mengabaikan klik yang dikirim Playwright/CDP pada
    tombol Buat (+) - ini sudah diuji dengan empat metode (locator.click,
    mouse down/up, focus+Enter, dispatch_event) dalam keadaan tanpa dialog
    penghalang dan tombol terbukti terekspos: keempatnya diabaikan. Yang
    diterima hanya klik level OS (CGEventPost lewat pyautogui), dan macOS
    cuma mengizinkannya untuk aplikasi yang punya izin Accessibility.
    Terminal.app punya izin itu; VS Code tidak.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

AKAR = Path(__file__).resolve().parent
sys.path.insert(0, str(AKAR))
os.environ.setdefault("AUTOUPLOAD_DATA_DIR", str(AKAR))

EKST_VIDEO = {".mp4", ".mov", ".m4v", ".webm"}
EKST_FOTO = {".jpg", ".jpeg", ".png", ".webp"}


def gagal(pesan: str) -> None:
    print(f"\nGAGAL: {pesan}", file=sys.stderr)
    raise SystemExit(1)


def periksa_izin_klik() -> None:
    """Berhenti sekarang kalau macOS tidak mengizinkan klik sintetis.

    Tanpa cek ini, skrip tetap berjalan beberapa menit lalu berhenti dengan
    "popover tidak muncul" - pesan yang menyesatkan, karena koordinatnya
    sebenarnya sudah tepat sasaran dan yang salah cuma izinnya. Membaca posisi
    kursor TIDAK butuh izin, menggerakkannya butuh; itulah cara mendeteksinya.
    """
    try:
        import pyautogui
    except Exception as exc:  # noqa: BLE001 - pesannya harus tetap jelas
        gagal(
            f"pyautogui tidak bisa diimpor ({exc}).\n"
            f"Pasang dengan: {sys.executable} -m pip install pyautogui"
        )

    pyautogui.FAILSAFE = False
    awal = pyautogui.position()
    pyautogui.moveTo(awal.x + 60, awal.y + 60, 0.2)
    time.sleep(0.3)
    bergerak = pyautogui.position() != awal
    pyautogui.moveTo(awal.x, awal.y, 0.2)
    if not bergerak:
        gagal(
            "macOS tidak mengizinkan skrip ini menggerakkan kursor, jadi klik "
            "tombol Buat (+) pasti diabaikan.\n\n"
            "  1. Buka System Settings -> Privacy & Security -> Accessibility\n"
            "  2. Centang Terminal (kalau belum ada, tambahkan dengan tombol +)\n"
            "  3. Tutup Terminal, buka lagi, jalankan ulang skrip ini\n\n"
            "Catatan: menjalankannya dari terminal VS Code tidak akan pernah "
            "berhasil kecuali VS Code sendiri diberi izin yang sama."
        )
    print("izin Accessibility: OK")


def cari_berkas(folder: Path) -> tuple[Path, str]:
    """Ambil satu media + captionnya dari folder."""
    if not folder.is_dir():
        gagal(f"Folder tidak ada: {folder}")

    media = sorted(
        (p for p in folder.iterdir()
         if p.is_file() and p.suffix.lower() in EKST_VIDEO | EKST_FOTO),
        key=lambda p: p.stat().st_mtime,
    )
    if not media:
        gagal(
            f"Tidak ada video/foto di {folder}\n"
            f"Taruh satu file ({', '.join(sorted(EKST_VIDEO | EKST_FOTO))}) di situ."
        )
    if len(media) > 1:
        nama = ", ".join(p.name for p in media)
        print(f"ada {len(media)} file di folder ({nama}); memakai yang terbaru")
    pilih = media[-1]

    for kandidat in (pilih.with_suffix(".txt"), folder / "caption.txt"):
        if kandidat.is_file():
            caption = kandidat.read_text(encoding="utf-8").strip()
            if caption:
                print(f"caption dibaca dari {kandidat.name}")
                return pilih, caption

    gagal(
        f"Caption untuk {pilih.name} tidak ketemu.\n"
        f"Buat salah satu file ini dan isi captionnya:\n"
        f"  {pilih.with_suffix('.txt')}\n"
        f"  {folder / 'caption.txt'}"
    )
    raise AssertionError("tidak tercapai")  # demi type checker


def ambil_ssid(ssid_arg: str) -> str:
    """Session ID dari argumen, atau dari akun Instagram aktif di database."""
    if ssid_arg:
        return ssid_arg

    from autoupload.db import SessionLocal, SocialAccount, init_db

    init_db()
    db = SessionLocal()
    try:
        akun = (
            db.query(SocialAccount)
            .filter(SocialAccount.platform == "instagram",
                    SocialAccount.is_active.is_(True))
            .order_by(SocialAccount.id.desc())
            .first()
        )
        if akun is None or not akun.session_id:
            gagal(
                "Belum ada akun Instagram aktif dengan Session ID.\n"
                "Isi Session ID di halaman Auto Upload, atau pakai --ssid."
            )
        print(f"akun: {akun.display_name or akun.session_name} (id {akun.id})")
        return akun.session_id
    finally:
        db.close()


async def unggah(media: Path, caption: str, ssid: str) -> str | None:
    from autoupload.browser import MediaKind
    from autoupload.platforms import REGISTRY

    kind = MediaKind.VIDEO if media.suffix.lower() in EKST_VIDEO else MediaKind.PHOTO
    adapter = REGISTRY["instagram"](session_name="default", headless=False)

    print(f"\nfile    : {media.name} ({media.stat().st_size / 1024:.0f} KB, {kind.value})")
    print(f"caption : {caption[:70]}{'...' if len(caption) > 70 else ''}")
    print("\nChrome akan terbuka dan kursor dipakai otomatis.")
    print("JANGAN sentuh mouse/keyboard sampai selesai.\n")

    hasil = await adapter.upload(media, caption, kind, session_id=ssid)
    if not hasil.success:
        gagal(hasil.error or "tidak diketahui")
    return hasil.url


def main() -> None:
    p = argparse.ArgumentParser(
        description="Unggah satu postingan Instagram dari folder berkas.")
    p.add_argument("--folder", default=str(AKAR / "berkas"),
                   help="folder berisi media + caption (default: berkas/)")
    p.add_argument("--video", help="pakai file ini, abaikan folder")
    p.add_argument("--caption", help="pakai caption ini, abaikan file .txt")
    p.add_argument("--ssid", default="", help="Session ID, kalau tidak dari database")
    p.add_argument("--sisakan", action="store_true",
                   help="jangan pindahkan file ke terkirim/ setelah berhasil")
    a = p.parse_args()

    if sys.platform != "darwin":
        gagal("Skrip ini khusus macOS (klik level OS lewat CGEvent).")

    # Validasi berkas dulu, baru izin: pengecekan izin menggerakkan kursor
    # sungguhan, jadi jangan sampai itu terjadi hanya untuk memberi tahu
    # bahwa path-nya salah ketik.
    folder = Path(a.folder).expanduser().resolve()
    if a.video:
        media = Path(a.video).expanduser().resolve()
        if not media.is_file():
            gagal(f"File tidak ada: {media}")
        caption = a.caption
        if not caption:
            sisi = media.with_suffix(".txt")
            if not sisi.is_file():
                gagal(f"Caption belum ada. Pakai --caption, atau buat {sisi}")
            caption = sisi.read_text(encoding="utf-8").strip()
    else:
        media, caption = cari_berkas(folder)
        if a.caption:
            caption = a.caption

    if not caption.strip():
        gagal("Caption kosong.")

    ssid = ambil_ssid(a.ssid)
    periksa_izin_klik()
    url = asyncio.run(unggah(media, caption, ssid))

    print(f"\nBERHASIL{': ' + url if url else ' (URL tidak terbaca)'}")

    # Dipindahkan supaya menjalankan skrip dua kali tidak memposting ulang
    # file yang sama secara tidak sengaja.
    if not a.sisakan and media.parent == folder:
        terkirim = folder / "terkirim"
        terkirim.mkdir(exist_ok=True)
        for asal in (media, media.with_suffix(".txt")):
            if asal.is_file():
                shutil.move(str(asal), str(terkirim / asal.name))
        print(f"file dipindahkan ke {terkirim}")


if __name__ == "__main__":
    main()
