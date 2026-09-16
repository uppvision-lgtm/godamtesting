import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { env } from '../config.js';
import { sessionRequest, sessionError, looksLoggedOut } from '../cookies.js';

/**
 * ── Endpoint web YouTube Studio (bukan API resmi berbayar) ───────────────
 *
 * Cek sesi    : POST www.youtube.com/youtubei/v1/account/account_menu
 * Upload      : PUT  upload.youtube.com/upload?upload_id=…  (resumable / scotty)
 * Judul dll   : POST studio.youtube.com/youtubei/v1/video_manager/metadata_update
 *
 * Autentikasinya memakai SAPISIDHASH: sha1(timestamp + " " + SAPISID + " " + origin).
 * Karena itu cookie SAPISID wajib ada dan origin harus persis sama dengan yang
 * dipakai saat menandatangani.
 *
 * Kalau YouTube mengubah alur upload-nya, ubah konstanta di bawah.
 */
const UPLOAD_BASE = env('YT_UPLOAD_URL', 'https://upload.youtube.com/upload');
const YT_INNERTUBE = env('YT_INNERTUBE', 'https://www.youtube.com/youtubei/v1');
const STUDIO_INNERTUBE = env('YT_STUDIO_INNERTUBE', 'https://studio.youtube.com/youtubei/v1');
const INNERTUBE_KEY = env('YT_INNERTUBE_KEY', 'AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8');
const ORIGIN = 'https://studio.youtube.com';

const CLIENT = { clientName: 'WEB', clientVersion: '2.20240903.01.00', hl: 'en', gl: 'US' };

/** SAPISIDHASH — satu-satunya bentuk autentikasi cookie yang diterima Google. */
function sapiHeaders(cookies, origin) {
  const sapisid = cookies.SAPISID || cookies['__Secure-3PAPISID'];
  if (!sapisid) {
    throw new Error('Cookie SAPISID tidak ditemukan. Salin ulang cookie dari youtube.com (lihat daftar wajib).');
  }
  const ts = Math.floor(Date.now() / 1000);
  const hash = crypto.createHash('sha1').update(`${ts} ${sapisid} ${origin}`).digest('hex');
  return { Authorization: `SAPISIDHASH ${ts}_${hash}`, 'X-Origin': origin, Origin: origin };
}

/** Ambil nama channel dari respons account_menu (bentuknya bersarang dalam). */
function pickChannelName(data) {
  const action = data?.actions?.find((a) => a?.openPopupAction);
  const account =
    action?.openPopupAction?.popup?.multiPageMenuRenderer?.header?.activeAccountHeaderRenderer;
  return (
    account?.accountName?.simpleText ||
    account?.channelHandle?.simpleText ||
    account?.accountPhoto?.thumbnails?.[0]?.url?.match(/\/channel\/([\w-]+)/)?.[1] ||
    null
  );
}

export default {
  id: 'youtube',

  session: {
    intro:
      'YouTube butuh cukup banyak cookie, dan semuanya harus dari google.com saat kamu login ke akun yang punya channel itu.',
    steps: [
      'Buka youtube.com dan pastikan kamu login ke akun Google yang benar (buka youtube.com/account untuk memastikan).',
      'Tekan F12 → Application (Chrome) atau Storage (Firefox) → Cookies → https://www.youtube.com.',
      'Salin nilai keempat kolom wajib: SID, HSID, SSID, SAPISID.',
      'SAPISID adalah yang paling penting — dari situ tanda tangan autentikasi dibuat.',
      'Kalau muncul error soal sesi, biasanya HSID/SSID ikut berubah saat kamu logout; salin ulang keempatnya sekaligus.',
    ],
    fields: [
      { key: 'SID', label: 'SID', required: true },
      { key: 'HSID', label: 'HSID', required: true },
      { key: 'SSID', label: 'SSID', required: true },
      { key: 'SAPISID', label: 'SAPISID', required: true },
      { key: '__Secure-1PSID', label: '__Secure-1PSID', required: false },
      { key: '__Secure-3PSID', label: '__Secure-3PSID', required: false },
      { key: '__Secure-1PAPISID', label: '__Secure-1PAPISID', required: false },
      { key: '__Secure-3PAPISID', label: '__Secure-3PAPISID', required: false },
    ],
  },

  async verify({ cookies }) {
    const r = await sessionRequest(`${YT_INNERTUBE}/account/account_menu?key=${INNERTUBE_KEY}&alt=json`, {
      method: 'POST',
      cookies,
      headers: { ...sapiHeaders(cookies, 'https://www.youtube.com'), 'Content-Type': 'application/json' },
      json: { context: { client: CLIENT } },
    });

    if (looksLoggedOut(r) || r.status === 401 || r.status === 403) {
      throw new Error('Cookie YouTube ditolak. Pastikan SID/HSID/SSID/SAPISID disalin lengkap dan belum logout.');
    }
    if (!r.ok) throw sessionError('Verifikasi YouTube', r);

    const displayName = pickChannelName(r.data);
    if (!displayName) {
      throw new Error('Sesi diterima, tapi channel tidak terbaca. Pastikan akun Google ini punya channel YouTube.');
    }
    return { cookies: r.cookies, displayName, platformUserId: displayName };
  },

  async publish({ account, filePath, caption, title, options = {}, log, onProgress }) {
    const cookies = account.cookies;
    const size = fs.statSync(filePath).size;
    const privacy = (options.youtubePrivacy || 'private').toUpperCase();
    const name = path.basename(filePath);

    log(`Mengunggah ${(size / 1024 / 1024).toFixed(1)} MB ke YouTube Studio…`);
    const uploadId = crypto.randomUUID();

    const r = await sessionRequest(`${UPLOAD_BASE}?upload_id=${uploadId}`, {
      method: 'PUT',
      cookies,
      headers: {
        ...sapiHeaders(cookies, ORIGIN),
        'X-Goog-Upload-Command': 'upload, finalize',
        'X-Goog-Upload-Protocol': 'resumable',
        'X-Goog-Upload-Offset': '0',
        'X-Goog-Upload-File-Name': name,
        'X-Goog-Upload-Header-Content-Length': String(size),
        'Content-Type': 'application/octet-stream',
      },
      body: fs.readFileSync(filePath),
    });

    if (!r.ok) throw sessionError('Upload ke YouTube', r);
    onProgress?.(80);

    const videoId = findVideoId(r);
    if (!videoId) {
      throw new Error(
        'Video terkirim, tapi YouTube tidak mengembalikan ID video. Cek YouTube Studio → Your videos, ' +
          'lalu sesuaikan cara membaca respons di src/platforms/youtube.js.',
      );
    }
    log(`Video masuk sebagai ${videoId}.`);

    // Judul/deskripsi/privacy bersifat best-effort: kalau gagal, video tetap ada
    // di Studio dan bisa diatur manual tanpa upload ulang.
    try {
      await applyMetadata({ cookies, videoId, title: title || caption || name, caption, privacy });
      log(`Judul, deskripsi, dan privacy (${privacy}) diatur.`);
    } catch (err) {
      log(`⚠ Metadata otomatis gagal (${err.message}). Atur judul & privacy manual di YouTube Studio.`);
    }

    return { id: videoId, url: `https://youtu.be/${videoId}`, message: `Terunggah ke YouTube (privacy: ${privacy}).` };
  },
};

/** ID video bisa datang dari body JSON, body teks, atau header upload. */
function findVideoId(r) {
  const fromJson = r.data?.videoId || r.data?.scottyResourceId || r.data?.id;
  if (fromJson) return fromJson;

  const fromText = /"videoId"\s*:\s*"([\w-]{11})"/.exec(r.text || '')?.[1];
  if (fromText) return fromText;

  const header = r.res?.headers?.get?.('x-goog-upload-url') || '';
  return /[?&]video_id=([\w-]{11})/.exec(header)?.[1] || null;
}

async function applyMetadata({ cookies, videoId, title, caption, privacy }) {
  const r = await sessionRequest(`${STUDIO_INNERTUBE}/video_manager/metadata_update?key=${INNERTUBE_KEY}&alt=json`, {
    method: 'POST',
    cookies,
    headers: { ...sapiHeaders(cookies, ORIGIN), 'Content-Type': 'application/json' },
    json: {
      context: { client: { ...CLIENT, clientName: 'WEB_STUDIO' } },
      videoId,
      title: { newTitle: title.slice(0, 95) },
      description: { newDescription: caption || '' },
      privacy: { newPrivacy: privacy },
    },
  });
  if (!r.ok) throw sessionError('Mengatur metadata YouTube', r);
}
