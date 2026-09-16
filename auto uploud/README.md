# 🎬 Auto Upload Sosmed

Web lokal untuk mengirim **satu video ke 6 sosial media sekaligus**:
Instagram Reels · TikTok · Facebook Page · Threads · X (Twitter) · YouTube Shorts.

**Multi-pengguna** — tiap orang punya akun sendiri di aplikasi ini, menautkan
sosmed-nya sendiri, dan tidak bisa melihat milik orang lain.

**Tanpa OAuth, tanpa daftar app, tanpa perantara berbayar.** Yang dipakai adalah
**sesi login (cookie) milikmu sendiri**, yang kamu tempelkan sekali lewat tombol
*Hubungkan*. Tidak ada app yang perlu direview, tidak ada redirect URI, tidak ada
langganan bulanan, dan tidak ada batas jumlah post.

---

## Menjalankan

```bash
npm install
cp .env.example .env
node -e "console.log(require('crypto').randomBytes(32).toString('hex'))"   # → APP_SECRET
npm start                            # buka http://localhost:4000
```

Selesai — tidak ada kredensial app yang perlu diisi. Semua platform langsung
tersedia; yang kamu butuhkan hanya cookie dari browser. Isi satu platform, itu
langsung bisa dipakai, sisanya menyusul.

---

## Cara pakai

1. **Daftar** di halaman depan. Akun pertama otomatis jadi admin.
2. **Hubungkan akun** — klik *Hubungkan* di kartu platform, lalu **tempel cookie**
   yang diminta. Jendela yang muncul memuat langkah persisnya: cookie mana saja,
   dan di mana menemukannya di DevTools. Cookie langsung dicek ke platform, jadi
   kalau salah salin kamu tahu saat itu juga, bukan nanti saat upload.
   Ada juga tombol *Cek ulang* untuk memastikan sesi masih hidup, dan *Putuskan*
   untuk menghapus cookie-nya.
3. **Tarik video** dari Finder ke kotak upload.
4. **Isi judul & caption**, centang platform tujuan, klik Upload.

Semua platform diproses **paralel**, masing-masing dengan log sendiri. Satu
gagal tidak menghentikan yang lain.

### Cookie apa saja yang dibutuhkan

Ringkasnya seperti ini (versi lengkap per platform muncul di UI):

| Platform | Cookie |
|---|---|
| Instagram | `sessionid`, `ds_user_id`, `csrftoken` |
| Threads | `sessionid`, `ds_user_id`, `csrftoken` |
| TikTok | `sessionid`, `sid_tt`, `uid_tt`, `ttwid` |
| Facebook | `c_user`, `xs` (+ `datr`, `sb`) dan **Page ID** |
| X | `auth_token`, `ct0` |
| YouTube | `SID`, `HSID`, `SSID`, `SAPISID` |

Semua nilainya ada di **DevTools → Application (Chrome) / Storage (Firefox) →
Cookies**. Dua aturan yang berlaku untuk semuanya:

- **Jangan menekan Log out** di browser itu selama aplikasi ini dipakai. Log out
  membatalkan cookie yang sama, termasuk yang tersimpan di sini.
- Menekan *Putuskan* hanya menghapus salinan di server ini; sesi di browser-mu
  tidak tersentuh.

---

## Tidak perlu tunnel lagi

Versi sebelumnya memakai Graph API Meta, yang mengharuskan Instagram dan Threads
mengunduh video dari sebuah URL publik — sehingga butuh cloudflared/ngrok.
Sekarang keduanya menerima **kiriman file langsung**, sama seperti platform lain.
Jadi `PUBLIC_BASE_URL` sudah tidak ada lagi, dan server ini cukup jalan di
localhost.

---

## `doc_id` — dua platform yang perlu satu nilai tambahan

Lima platform lain sudah bisa dipakai begitu saja. Hanya **Facebook** dan
**Threads** yang perlu satu nilai dari kamu, karena keduanya tidak punya endpoint
posting publik yang bisa dipakai dengan cookie: web mereka memakai GraphQL
internal dengan `doc_id`, dan angka itu berubah setiap kali platformnya
diperbarui.

Ambilnya 30 detik:

1. Buka facebook.com (atau threads.com), tekan F12 → tab **Network**, filter `graphql`.
2. Posting satu video secara manual seperti biasa.
3. Klik request yang muncul — `doc_id=<angka panjang>` ada di URL-nya.
4. Salin angka itu ke `.env`:
   - Facebook → `FB_DOC_ID_VIDEO_UPLOAD`
   - Threads → `THREADS_DOC_ID_CREATE_POST`
5. Restart server.

Kalau nanti upload Facebook/Threads mulai gagal dengan pesan yang menyebut
`doc_id`, ulangi langkah di atas — biasanya cukup itu.

---

## Yang perlu diketahui per platform

- **Instagram** & **Threads** — akun Instagram harus **Professional
  (Business/Creator)** untuk Reels. Cookie Threads diambil dari threads.com,
  bukan instagram.com.
- **Facebook** — video hanya bisa diposting ke **Page**, bukan profil pribadi.
  Page ID dicari di Page-mu → menu ⋯ → About/Transparansi halaman, atau langsung
  tempel URL halaman di kolom yang tersedia.
- **X** — tidak ada biaya dan tidak ada langganan di jalur ini, karena memakai
  sesi web-mu. Cookie `ct0` wajib: tanpa itu semua request dijawab 403.
- **TikTok** — privacy diatur per upload dari dropdown *Opsi lanjutan*
  (publik / teman / hanya saya). Tidak ada batas `SELF_ONLY` seperti di API resmi.
- **YouTube** — `SAPISID` adalah cookie paling penting; dari situ tanda tangan
  autentikasi dibuat. Kalau judul/deskripsi gagal diatur otomatis, video tetap
  terunggah dan bisa kamu lengkapi manual di YouTube Studio (log job akan
  memberi tahu).

---

## Batasan & risiko

Ini bagian yang perlu kamu baca sebelum dipakai sehari-hari. Mode sesi berarti
aplikasi ini bertindak seperti kamu yang sedang membuka browser, memakai titik
akhir internal platform — bukan API resmi yang mereka sediakan untuk pengembang.

- **Tidak diizinkan oleh syarat layanan platform.** Otomasi lewat sesi pribadi
  umumnya melanggar ToS. Konsekuensinya bisa berupa pembatasan sementara sampai
  penangguhan akun. Gunakan untuk akunmu sendiri, dengan volume wajar.
- **Rentan berubah.** Kalau platform memperbarui web-nya, endpoint bisa bergeser
  dan upload gagal. Semua endpoint dan `doc_id` sengaja dikumpulkan sebagai
  konstanta di bagian atas tiap file `src/platforms/*.js` supaya perbaikannya
  cukup satu baris. Log job selalu menampilkan respons mentah platform untuk
  memudahkan penelusuran.
- **Cookie = kredensial penuh.** Siapa pun yang bisa membaca `data/db.json`
  memegang akses ke akunmu. Jangan bagikan folder `data/` atau `.env`, dan
  jalankan hanya di komputer yang kamu percayai.
- **Yang belum teruji di sini:** Facebook dan Threads. Keduanya bergantung pada
  `doc_id` dan bentuk variabel GraphQL yang berbeda-beda antar versi web. Kalau
  gagal, pesan errornya menyebut apa yang harus diambil dari DevTools.

Kalau kamu butuh jalur yang stabil jangka panjang dan siap dipakai banyak orang,
itu justru alasan OAuth + App Review ada — lihat `REVIEW.md` untuk jalur itu.

---

## Struktur

```
src/
  server.js              Express + static + error handler
  config.js              .env, daftar 6 platform
  db.js                  database JSON: users, sessions, cookie sosmed per user
  session.js             cookie sesi bertanda tangan HMAC, guard auth/admin
  cookies.js             cookie jar + fetch yang menyerupai browser
  social.js              ambil koneksi platform sebelum upload
  http.js                helper fetch + polling status
  queue.js               job upload, semua platform paralel, event untuk SSE
  platforms/             satu file per platform: session (field + langkah), verify, publish
  routes/
    auth.js              daftar, masuk, keluar
    social.js            status, connect (verify + simpan), cek ulang, putuskan
    upload.js            terima file, buat job, stream progres
public/                  UI (HTML + CSS + JS, tanpa build step)
reference/               arsip: implementasi lama lewat provider pihak ketiga
legacy/                  arsip: implementasi lama berbasis OAuth
```

Menambah platform baru = satu file di `platforms/` dengan `session` (daftar field
+ langkah pengambilan cookie), `verify()` untuk mengecek sesi, dan `publish()`
untuk mengunggah — lalu daftarkan di `platforms/index.js` dan `config.js`.
UI membangun form cookie-nya otomatis dari `session.fields`, jadi tidak ada
yang perlu diubah di `public/`.

---

## Kenapa mode sesi, bukan OAuth?

Jalur resmi (OAuth + App Review) memberi kestabilan, tapi harganya mahal untuk
pemakaian pribadi:

- Setiap platform minta app didaftarkan, dengan redirect URI terdaftar dan
  review izin sebelum bisa dipakai orang lain. Meta menambah syarat verifikasi
  bisnis; TikTok mengunci post ke `SELF_ONLY` sampai audit lolos; X sejak 2026
  berbayar per post; YouTube membatasi kuota harian.
- Artinya untuk memakai sendiri pun kamu harus mengurus enam pengajuan, dan
  sebagian di antaranya tidak akan lolos tanpa badan usaha.

Mode sesi memangkas semua itu: tempel cookie, langsung jalan, tanpa batas post
dan tanpa biaya. Yang kamu tukar adalah kestabilan dan kepatuhan ToS — lihat
[Batas & risiko](#batasan--risiko) di atas. Kalau suatu saat aplikasi ini dibuka
untuk umum, jalur OAuth-lah yang masuk akal; implementasinya masih tersimpan di
`legacy/` bersama arsip provider pihak ketiga di `reference/`.
