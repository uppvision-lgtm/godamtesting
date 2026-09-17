"""Penjawab kesehatan minimal untuk container yang hanya menjalankan worker.

Railway memeriksa kesehatan lewat HTTP (lihat healthcheckPath di
railway.json). Worker render tidak punya server web, jadi tanpa ini layanan
worker akan dianggap gagal dan di-restart terus-menerus.

Sengaja dibuat sekecil mungkin: tidak mengimpor FastAPI, tidak menyentuh
database, tidak menyentuh Supabase. Tugasnya cuma menjawab bahwa prosesnya
hidup. Kalau worker-nya sendiri mati, ``start.sh`` menghentikan proses ini
juga sehingga Railway tetap tahu ada yang salah.
"""

from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Penjawab(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - nama wajib dari pustaka
        isi = b'{"status":"ok","role":"worker"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(isi)))
        self.end_headers()
        self.wfile.write(isi)

    def log_message(self, *_args: object) -> None:
        """Diam. Pemeriksaan kesehatan berjalan tiap beberapa detik dan
        mencatatnya hanya membanjiri log tanpa memberi informasi apa pun."""


def main() -> None:
    port = int(os.getenv("PORT", "8000"))
    ThreadingHTTPServer(("0.0.0.0", port), Penjawab).serve_forever()


if __name__ == "__main__":
    main()
