# Auto Upload

Upload satu video/foto, sebar otomatis ke **TikTok, Instagram, Facebook, YouTube,
Threads, dan X/Twitter** — lewat sesi browser nyata (Playwright), tanpa API resmi
yang berbayar/ribet approval.

## Isi

```
autoupload/              # paket Python — ini "script"-nya
├── uploader.py          # orchestrator: fan-out paralel ke banyak platform
├── browser.py           # mesin Playwright (profil persisten + anti-deteksi)
├── platforms/           # 6 adapter: alur posting tiap platform
├── login.py             # login manual lewat jendela browser, sesi disimpan
├── service.py           # simpan post ke DB, jalankan, penjadwal
├── caption.py           # potong caption sesuai limit tiap platform
├── db.py                # 3 tabel SQLAlchemy
├── api.py               # 1 APIRouter (semua endpoint)
├── config.py            # konfigurasi via environment variable
└── static/index.html    # UI 1 file, HTML+JS polos, tanpa build step
main.py                  # runner mandiri (opsional)
```

## Install

```bash
pip install -r requirements.txt
playwright install chromium     # sekali saja
```

## Cara pakai

### A. Dipanggil langsung dari kode Python (Flask / Django / cron / CLI)

Tanpa HTTP, tanpa FastAPI:

```python
from autoupload import init_db, upload_sync

init_db()                                    # sekali saat start aplikasi
hasil = upload_sync(
    "uploads/video.mp4",
    "Caption gue #viral",
    ["tiktok", "instagram", "twitter"],       # jalan paralel
)
for r in hasil:
    print(r.platform, r.success, r.url or r.error)
```

Contoh di view Flask:

```python
@app.post("/posting")
def posting():
    hasil = upload_sync(request.form["path"], request.form["caption"], ["tiktok"])
    return {"hasil": [vars(r) for r in hasil]}
```

Django/Flask yang sudah punya event loop (async view) pakai versi async-nya:

```python
from autoupload import fan_out_upload, MediaKind
hasil = await fan_out_upload(path, caption, MediaKind.VIDEO, [{"platform": "tiktok"}])
```

### B. Di-mount ke website FastAPI yang sudah ada

```python
from autoupload import router, init_db, start_scheduler

init_db()
start_scheduler()              # jalankan post terjadwal (thread latar, tanpa Redis)
app.include_router(router)     # semua endpoint di bawah /api
```

Lindungi endpoint-nya dengan login website utamamu, mis.:

```python
app.include_router(router, dependencies=[Depends(login_kamu)])
```

### C. Jalan sendiri sebagai service kecil

```bash
python main.py                 # buka http://127.0.0.1:8000
```

Buka alamat itu: pilih file → caption → centang platform → Upload.

## Front end

`autoupload/static/index.html` — satu file HTML + CSS + JS polos. Tanpa build
step, tanpa npm, tanpa framework. Isinya: kelola sesi login per platform,
drag-and-drop file, penghitung karakter per platform, pilih tujuan, jadwal,
dan tabel riwayat yang menyegarkan sendiri. Ikut mode gelap OS dan jalan
sampai layar 400px.

`python main.py` sudah melayaninya di `/`. Untuk menempelkannya ke website lain:

**Flask** — copy file itu ke `templates/autoupload.html`, lalu:

```python
@app.get("/autoupload")
def halaman_autoupload():
    return render_template("autoupload.html")
```

**Django** — taruh di `templates/autoupload.html`, lalu `TemplateView.as_view(template_name="autoupload.html")`.

Kalau API-nya beda domain/port dari halamannya, arahkan sebelum `<script>`-nya jalan:

```html
<script>window.AUTOUPLOAD_API = "http://127.0.0.1:8000/api"</script>
```

Kalau tidak diset, dia memanggil `/api` di domain yang sama. Beda domain berarti
kamu perlu mengizinkan CORS di sisi API.

## Endpoint

| Method | Path | Fungsi |
|---|---|---|
| POST | `/api/media` | unggah file, balas `media_path` |
| POST | `/api/upload` | upload sekarang atau jadwalkan (`scheduled_at` ISO) |
| GET | `/api/posts` | riwayat + jadwal |
| GET/POST/DELETE | `/api/accounts` | daftar akun tersimpan |
| GET | `/api/login` | status sesi browser tiap platform |
| POST | `/api/login/{platform}/start` \| `/finish` \| `/cancel` | alur login manual |
| DELETE | `/api/login/{platform}/{nama}` | hapus sesi |

## Menyambungkan akun

Dua cara, pilih salah satu:

1. **Login Browser** (disarankan) — `POST /api/login/{platform}/start` membuka
   jendela Chrome, kamu login manual di situ, lalu `POST .../finish`. Cookie
   disimpan permanen di `browser_profiles/<platform>/<nama>/` dan tahan
   berminggu-minggu. Sesi diverifikasi dulu (cek cookie login) sebelum disimpan.
2. **Session ID** — tempel nilai cookie login lewat `POST /api/accounts`. Untuk
   platform yang butuh beberapa cookie, gabungkan dengan `|` sesuai urutan di
   `session_cookie_map` pada adapter platformnya.

## Konfigurasi (environment variable, semua opsional)

| Variabel | Default | Keterangan |
|---|---|---|
| `AUTOUPLOAD_DATA_DIR` | `.` | folder untuk DB, `uploads/`, `browser_profiles/` |
| `AUTOUPLOAD_DATABASE_URL` | `sqlite:///<data>/autoupload.db` | boleh Postgres |
| `AUTOUPLOAD_MAX_FILE_MB` | `500` | batas ukuran file |
| `AUTOUPLOAD_SCHEDULER_INTERVAL` | `30` | detik antar cek jadwal |

**Penting saat merge:** kalau working directory website utamamu beda, set
`AUTOUPLOAD_DATA_DIR` ke folder tempat `browser_profiles/` berada, kalau tidak
sesi login lamamu tidak terbaca.

## Catatan platform

- **Instagram** wajib headful (jendela terlihat) di macOS + `pyautogui`: IG
  mengabaikan klik dari Playwright pada tombol "Buat", hanya menerima klik level
  OS. Platform lain jalan headless.
- **YouTube** butuh UA Chrome asli; headless mentah kena interstitial
  "browser tidak didukung" dan tombol Publish tidak muncul (sudah ditangani).
- Kalau upload gagal, screenshot + teks halaman tersimpan di `debug_shots/`
  untuk melacak selector yang berubah.
