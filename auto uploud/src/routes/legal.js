import crypto from 'node:crypto';
import express from 'express';
import { APP_NAME, APP_CONTACT_EMAIL, APP_OWNER, BASE_URL, PLATFORMS, META_APP_SECRET } from '../config.js';
import { findUserByPlatformUserId, removeConnection, recordDeletion, readDeletion } from '../db.js';

const router = express.Router();

const contact = () => APP_CONTACT_EMAIL || '(isi APP_CONTACT_EMAIL di .env)';
const owner = () => APP_OWNER || APP_NAME;
const names = PLATFORMS.map((p) => p.name).join(', ');

/* ── Halaman legal ─────────────────────────────────────────────────────── */

function shell(title, body) {
  return `<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${title} · ${APP_NAME}</title><link rel="stylesheet" href="/styles.css"></head>
<body><main class="wrap legal">
<p class="hint"><a href="/">← ${APP_NAME}</a></p>
<h1>${title}</h1>${body}
<p class="hint">Pertanyaan: <a href="mailto:${contact()}">${contact()}</a></p>
</main></body></html>`;
}

router.get('/privacy', (req, res) => {
  res.type('html').send(
    shell(
      'Kebijakan Privasi',
      `
<p class="hint">Terakhir diperbarui: ${new Date().toISOString().slice(0, 10)}</p>

<h2>Ringkasnya</h2>
<p>${APP_NAME} membantu kamu mengunggah satu video ke beberapa akun sosial mediamu sekaligus.
Kami hanya menyimpan yang diperlukan untuk itu, dan kamu bisa menghapusnya kapan saja.
<b>Kami tidak pernah meminta atau menyimpan password akun sosial mediamu.</b></p>

<h2>Data yang kami simpan</h2>
<ul>
  <li><b>Akun aplikasi</b> — alamat email, nama, dan password yang disimpan dalam bentuk hash
      (scrypt). Password aslinya tidak dapat dibaca kembali oleh siapa pun.</li>
  <li><b>Cookie sesi sosial media</b> — nilai cookie yang kamu tempelkan sendiri saat
      menghubungkan akun (${names}), beserta nama tampilan dan id akun agar kami tahu
      harus mengunggah ke mana. Cookie ini tersimpan hanya di komputer tempat aplikasi ini
      berjalan dan tidak pernah dikirim ke browser. Perlu kamu tahu: cookie sesi itu
      <b>setara dengan akses penuh ke akunmu</b> — siapa pun yang membacanya bisa bertindak
      sebagai kamu di platform tersebut.</li>
  <li><b>Video yang kamu unggah</b> — disimpan sementara di server hanya selama proses
      pengiriman berlangsung, lalu dihapus otomatis setelah selesai.</li>
  <li><b>Judul dan caption</b> yang kamu ketik, karena itu ikut dikirim ke platform tujuan.</li>
</ul>

<h2>Yang tidak kami lakukan</h2>
<ul>
  <li>Kami tidak meminta atau menyimpan password akun sosial mediamu.</li>
  <li>Kami tidak mengirimkan cookie-mu ke pihak ketiga mana pun selain platform yang
      bersangkutan.</li>
  <li>Kami tidak menjual, menyewakan, atau membagikan datamu ke pihak lain.</li>
  <li>Kami tidak membaca, mengunduh, atau menganalisis isi akun sosial mediamu di luar
      yang dibutuhkan untuk mengunggah dan menampilkan status unggahan.</li>
  <li>Kami tidak memakai datamu untuk iklan atau pelatihan model.</li>
</ul>

<h2>Ke mana data dikirim</h2>
<p>Video, judul, dan caption yang kamu kirim diteruskan langsung ke platform tujuan yang
kamu pilih sendiri, dengan memakai sesi login yang kamu serahkan tadi. Setelah sampai di
sana, data tersebut tunduk pada kebijakan privasi masing-masing platform. Tidak ada
perantara lain yang terlibat.</p>

<h2>Berapa lama disimpan</h2>
<ul>
  <li>Video: dihapus segera setelah seluruh proses unggah selesai.</li>
  <li>Cookie sesi: tersimpan sampai kamu menekan Putuskan, menghapus akunmu, atau
      sesi itu mati sendiri di sisi platform.</li>
  <li>Akun aplikasi: tersimpan sampai kamu menghapusnya.</li>
</ul>

<h2>Hak kamu</h2>
<p>Kamu bisa mencabut akses satu platform lewat tombol <b>Putuskan</b>, atau menghapus
seluruh akun beserta semua cookie sesi lewat menu <b>Hapus akun</b> di aplikasi. Penghapusan
berlaku seketika dan tidak bisa dibatalkan. Kamu juga bisa langsung mengakhiri seluruh sesi
dengan menekan Log out di browser atau dari pengaturan keamanan platform yang bersangkutan.
</p>
<p>Petunjuk lengkap ada di <a href="/data-deletion">halaman penghapusan data</a>.</p>

<h2>Keamanan</h2>
<p>Password disimpan sebagai hash scrypt dengan salt acak. Sesi memakai cookie HttpOnly
yang ditandatangani HMAC. Cookie sesi sosial mediamu tidak pernah dikirimkan ke sisi browser.
Karena cookie itu setara kredensial, jangan bagikan folder <code>data/</code> atau file
<code>.env</code> milik server ini ke siapa pun, dan jalankan aplikasi ini hanya di komputer
yang kamu percayai.</p>

<h2>Penanggung jawab</h2>
<p>${owner()} — <a href="mailto:${contact()}">${contact()}</a></p>`,
    ),
  );
});

router.get('/terms', (req, res) => {
  res.type('html').send(
    shell(
      'Syarat Penggunaan',
      `
<p class="hint">Terakhir diperbarui: ${new Date().toISOString().slice(0, 10)}</p>

<h2>Layanan ini</h2>
<p>${APP_NAME} adalah alat bantu untuk mengunggah video ke akun sosial media milikmu sendiri
(${names}). Layanan disediakan apa adanya, tanpa jaminan ketersediaan.</p>

<h2>Cara kerjanya & risikonya</h2>
<p>Aplikasi ini bekerja dengan memakai sesi login (cookie) yang kamu serahkan sendiri. Artinya
ia bertindak seperti kamu yang sedang membuka browser. Cara ini memakai titik akhir internal
platform, bukan API resmi yang mereka sediakan untuk pengembang, sehingga:</p>
<ul>
  <li>Platform dapat mengubah atau menutup jalur ini kapan saja, dan unggahan bisa mendadak
      gagal sampai kode di aplikasi ini disesuaikan.</li>
  <li>Otomasi lewat sesi pribadi umumnya <b>tidak diizinkan</b> oleh syarat layanan platform.
      Konsekuensinya bisa berupa pembatasan sementara sampai penangguhan akun.</li>
  <li>Pakai untuk akunmu sendiri atau akun yang kamu beri izin mengelolanya, dengan volume
      yang wajar — hindari pola spam.</li>
</ul>
<p>Kami tidak bertanggung jawab atas tindakan yang diambil platform terhadap akunmu.</p>

<h2>Kewajiban kamu</h2>
<ul>
  <li>Hanya menautkan akun yang benar-benar milikmu atau yang kamu punya izin mengelolanya.</li>
  <li>Hanya mengunggah konten yang kamu punya haknya.</li>
  <li>Mematuhi syarat dan ketentuan tiap platform tujuan. Pelanggaran di sana ditindak oleh
      platform tersebut, bukan oleh kami.</li>
  <li>Tidak memakai layanan ini untuk spam, konten ilegal, atau tindakan yang melanggar
      aturan platform maupun hukum yang berlaku.</li>
</ul>

<h2>Batas tanggung jawab</h2>
<p>Kami tidak bertanggung jawab atas unggahan yang gagal, tertunda, atau ditolak oleh
platform tujuan, maupun atas tindakan yang diambil platform terhadap akunmu.</p>

<h2>Penghentian</h2>
<p>Kamu boleh berhenti kapan saja dengan menghapus akunmu. Kami dapat menghentikan akses
jika layanan dipakai melanggar syarat di atas.</p>`,
    ),
  );
});

router.get('/data-deletion', (req, res) => {
  const record = req.query.code ? readDeletion(String(req.query.code)) : null;
  const status = req.query.code
    ? record
      ? `<div class="notice ok-notice"><b>Permintaan selesai.</b> Data yang diperoleh dari
         ${record.platform} untuk akun ini sudah dihapus pada ${record.at}.</div>`
      : `<div class="notice"><b>Kode tidak ditemukan.</b> Kode penghapusan hanya berlaku
         untuk permintaan yang pernah kami terima.</div>`
    : '';

  res.type('html').send(
    shell(
      'Penghapusan Data',
      `${status}
<h2>Menghapus data kamu</h2>
<p>Ada tiga cara, semuanya berlaku seketika:</p>
<ol>
  <li><b>Lepas satu platform</b> — masuk ke ${APP_NAME}, lalu tekan <b>Putuskan</b> pada
      kartu platform yang bersangkutan. Cookie sesi untuk platform itu langsung dihapus
      dari server ini.</li>
  <li><b>Hapus seluruh akun</b> — masuk ke ${APP_NAME}, tekan <b>Hapus akun</b>. Seluruh
      data kamu — akun, sesi aplikasi, dan semua cookie sosial media — dihapus permanen.</li>
  <li><b>Lewat platformnya</b> — tekan Log out di browser, atau akhiri sesi dari pengaturan
      keamanan platform tersebut. Setelah itu cookie yang tersimpan di sini otomatis tidak
      berlaku lagi, dan kamu bisa menekan <b>Putuskan</b> untuk menghapus sisanya.</li>
</ol>

<h2>Kalau tidak bisa masuk</h2>
<p>Kirim email ke <a href="mailto:${contact()}">${contact()}</a> dari alamat email yang kamu
pakai mendaftar. Permintaan diproses paling lambat 30 hari.</p>

<h2>Apa yang dihapus</h2>
<p>Semua cookie sesi platform, nama tampilan dan id akun platform, alamat email, nama, dan hash
password. Video tidak perlu dihapus terpisah karena sudah otomatis terhapus setiap kali
proses unggah selesai.</p>`,
    ),
  );
});

/* ── Callback hapus data dari Meta ─────────────────────────────────────── */

/**
 * Meta mengirim POST berisi signed_request kalau kamu mencabut app Meta-mu.
 * Endpoint ini hanya berguna kalau kamu juga punya app Meta sendiri
 * (META_APP_SECRET terisi di .env); mode sesi tidak memakai app Meta sama sekali.
 * Balasannya harus berupa URL status + kode konfirmasi.
 */
router.post('/api/data-deletion/meta', express.urlencoded({ extended: false }), (req, res) => {
  try {
    if (!META_APP_SECRET) throw new Error('META_APP_SECRET belum diisi di .env.');
    const payload = verifySignedRequest(req.body?.signed_request, META_APP_SECRET);
    const platformUserId = payload.user_id;
    if (!platformUserId) throw new Error('signed_request tanpa user_id.');

    // Satu akun Meta bisa menautkan Facebook dan Instagram sekaligus.
    let found = null;
    for (const platformId of ['facebook', 'instagram']) {
      const user = findUserByPlatformUserId(platformId, platformUserId);
      if (!user) continue;
      removeConnection(user.id, platformId);
      found = user;
    }

    const code = crypto.randomBytes(12).toString('hex');
    recordDeletion(code, { platform: 'Meta', platformUserId, userId: found?.id || null });

    res.json({ url: `${BASE_URL}/data-deletion?code=${code}`, confirmation_code: code });
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

/** Verifikasi tanda tangan HMAC-SHA256 milik Meta. */
function verifySignedRequest(signed, appSecret) {
  if (!signed || !appSecret) throw new Error('signed_request atau app secret tidak ada.');
  const [encodedSig, encodedPayload] = String(signed).split('.');
  if (!encodedSig || !encodedPayload) throw new Error('Bentuk signed_request tidak valid.');

  const expected = crypto.createHmac('sha256', appSecret).update(encodedPayload).digest();
  const actual = Buffer.from(encodedSig, 'base64url');
  if (actual.length !== expected.length || !crypto.timingSafeEqual(actual, expected)) {
    throw new Error('Tanda tangan signed_request tidak cocok.');
  }
  return JSON.parse(Buffer.from(encodedPayload, 'base64url').toString('utf8'));
}

export default router;
