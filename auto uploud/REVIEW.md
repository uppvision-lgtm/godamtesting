# Checklist App Review — menuju Live mode

> **Catatan:** dokumen ini berlaku untuk **jalur OAuth**, yaitu versi aplikasi
> yang memakai app milikmu sendiri di tiap platform. Versi yang aktif sekarang
> memakai **mode sesi/cookie** (lihat README), yang tidak butuh App Review sama
> sekali — cukup tempel cookie.
>
> Simpan dokumen ini untuk saat kamu ingin pindah ke jalur resmi, misalnya kalau
> aplikasi ini dibuka untuk banyak orang dan butuh kestabilan jangka panjang.
> Implementasi OAuth versi lama ada di `legacy/`.

Tujuan: **siapa pun bisa mendaftar dan menautkan akunnya sendiri, tanpa kamu
daftarkan satu per satu.** Itu hanya terjadi setelah tiap app lolos review dan
berstatus Live/Production.

Selama menunggu, aplikasi tetap bisa dipakai dalam Development mode dengan
mendaftarkan tester — jadi pekerjaan tidak berhenti.

---

## 0 · Prasyarat yang menghalangi semuanya

Reviewer akan **membuka aplikasimu sendiri** dan mencoba alurnya. `localhost`
tidak bisa mereka akses, jadi sebelum mengajukan review apa pun:

- [ ] **Domain** — beli satu nama domain (mis. `.com` atau `.id`).
- [ ] **Hosting publik dengan HTTPS** — aplikasi harus hidup 24 jam di alamat
      tetap. Railway, Render, Fly.io, atau VPS kecil sudah cukup.
- [ ] **Ganti `BASE_URL` dan `PUBLIC_BASE_URL`** di `.env` ke domain itu.
- [ ] **Daftarkan ulang semua redirect URI** di enam dashboard, ganti
      `http://localhost:4000` menjadi `https://domainmu.com`.
- [ ] **Ganti `APP_SECRET`** dengan nilai acak baru.
- [ ] Isi `APP_NAME`, `APP_OWNER`, `APP_CONTACT_EMAIL` di `.env` — nilai ini
      tampil di halaman legal yang dibaca reviewer.

Setelah domainnya jadi, tunnel cloudflared tidak diperlukan lagi: Instagram dan
Threads akan mengunduh video langsung dari domainmu.

---

## 1 · Yang sudah siap

Ini sudah ada di aplikasi dan tidak perlu kamu kerjakan lagi:

- [x] **Halaman Kebijakan Privasi** — `/privacy`
- [x] **Halaman Syarat Penggunaan** — `/terms`
- [x] **Halaman Penghapusan Data** — `/data-deletion`
- [x] **Callback hapus data Meta** — `POST /api/data-deletion/meta`,
      memverifikasi tanda tangan HMAC dan benar-benar menghapus token
- [x] **Tombol hapus akun** di aplikasi, menghapus akun + seluruh token
- [x] **Tombol Putuskan** per platform untuk mencabut satu izin
- [x] Password disimpan sebagai hash scrypt, token tidak pernah dikirim ke browser

Reviewer Meta dan TikTok mengecek poin-poin ini secara khusus. Yang sering bikin
aplikasi ditolak adalah halaman legal yang cuma tempelan sementara tombol
hapusnya tidak benar-benar berfungsi — di sini keduanya nyata.

---

## 2 · Yang harus kamu siapkan sendiri

- [ ] **Video demo layar** (2–4 menit, tanpa musik, berbahasa Inggris atau
      bertakarir). Isinya: daftar akun baru → klik Hubungkan → login ke platform
      → pilih video → isi caption → Upload → tunjukkan postingan yang jadi.
      Buat **satu video per platform** karena tiap reviewer hanya peduli
      platformnya sendiri.
- [ ] **Akun uji** untuk reviewer (email + password di aplikasimu), karena mereka
      perlu masuk tanpa akun sosmed pribadi.
- [ ] **Penjelasan tiap izin** — satu paragraf per permission, menjelaskan kenapa
      aplikasimu membutuhkannya. Contoh untuk `instagram_content_publish`:
      *"Digunakan untuk mengunggah video ke akun Instagram Professional milik
      pengguna itu sendiri, atas permintaan eksplisit mereka lewat tombol Upload."*

---

## 3 · Per platform

### Meta — Instagram, Facebook, Threads
Satu app, satu pengajuan, tiga platform sekaligus.

- [ ] **Business verification** — Meta meminta dokumen badan usaha resmi
      (akta, NPWP, atau izin usaha) dan verifikasi nomor telepon. **Ini bagian
      terberat jalur B**: tanpa badan usaha, verifikasi bisnis sulit lolos.
- [ ] Izin yang diminta: `instagram_basic`, `instagram_content_publish`,
      `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`,
      `publish_video`, `business_management`
- [ ] Isi URL privasi, syarat, dan hapus data di **App settings → Basic**
- [ ] Ajukan lewat **App Review → Permissions and Features**
- Perkiraan waktu: beberapa hari sampai beberapa minggu

### TikTok
- [ ] Ajukan **audit Content Posting API**. Sebelum lolos, semua post terkunci
      `SELF_ONLY` — tidak bisa dilihat siapa pun selain pemilik akun.
- [ ] Perlu demo video dan penjelasan alur
- Perkiraan waktu: paling lama di antara semuanya, bisa berminggu-minggu

### YouTube
- [ ] Naikkan OAuth consent screen dari **Testing** ke **Production**
- [ ] Scope `youtube.upload` termasuk sensitif, jadi butuh
      **verifikasi OAuth** dari Google (demo video + penjelasan)
- [ ] Tanpa verifikasi, aplikasi tetap jalan tapi memunculkan layar peringatan
      dan refresh token kedaluwarsa tiap 7 hari
- [ ] Untuk volume tinggi, ajukan **YouTube API quota extension** terpisah

### X (Twitter)
- [ ] Tidak ada App Review. Siapa pun bisa langsung menautkan akunnya.
- [ ] Yang perlu disiapkan hanya metode pembayaran, karena skema pay-per-use
      (±$0.015 per post)

---

## 4 · Urutan pengerjaan yang disarankan

1. Domain + hosting (tanpa ini tidak ada yang bisa diajukan)
2. X — langsung jalan, tanpa review
3. YouTube — verifikasi Google
4. Meta — paling banyak berkasnya, mulai dari business verification
5. TikTok — paling lama, ajukan paling awal kalau bisa paralel

Selama menunggu semuanya, jalankan Development mode dengan tester supaya
aplikasinya sudah benar-benar terpakai dan kamu punya bahan demo video.
