import fs from 'node:fs';
import { env } from '../config.js';
import { sessionRequest, sessionError, requireCookies, looksLoggedOut } from '../cookies.js';

/**
 * ── Endpoint web threads.com (bukan Threads API resmi) ───────────────────
 *
 * Cek sesi : GET  www.threads.com/@username  +  i.instagram.com/api/v1/users/{id}/info/
 * Upload   : POST www.threads.com/rupload_igvideo/{upload_id}   ← file mentah
 * Terbitkan: POST www.threads.com/api/graphql/   → doc_id + lsd + fb_dtsg
 *
 * Sama seperti Facebook: platform ini memakai GraphQL internal dengan `doc_id`
 * yang bisa berubah. Isi THREADS_DOC_ID_CREATE_POST di .env kalau posting gagal.
 *
 * Cara mengambil doc_id (30 detik):
 *   1. Buka threads.com, F12 → tab Network, filter "graphql".
 *   2. Posting satu video secara manual.
 *   3. Lihat request yang muncul: doc_id=<angka panjang> ada di URL-nya.
 *   4. Salin ke THREADS_DOC_ID_CREATE_POST di .env, restart server.
 */
const HOST = env('THREADS_HOST', 'https://www.threads.com');
const DOC_CREATE_POST = env('THREADS_DOC_ID_CREATE_POST', '');
const APP_ID = env('IG_APP_ID', '936619743392459');

const sessionHeaders = (cookies, extra = {}) => ({
  'X-IG-App-ID': APP_ID,
  'X-CSRFToken': cookies.csrftoken,
  'X-Requested-With': 'XMLHttpRequest',
  Referer: `${HOST}/`,
  Origin: HOST,
  ...extra,
});

function extractTokens(html = '') {
  return {
    lsd: /"LSD",\[\],\{"token":"([^"]+)"/.exec(html)?.[1] || null,
    fb_dtsg: /"DTSGInitialData",\[\],\{"token":"([^"]+)"/.exec(html)?.[1] || null,
  };
}

export default {
  id: 'threads',

  session: {
    intro:
      'Threads memakai akun Instagram-mu, jadi cookie-nya mirip. Video dikirim langsung sebagai file — tidak butuh tunnel publik.',
    steps: [
      'Buka threads.com dan login (biasanya lewat "Continue with Instagram").',
      'Tekan F12 → Application (Chrome) atau Storage (Firefox) → Cookies → https://www.threads.com.',
      'Salin sessionid, ds_user_id, dan csrftoken.',
      'Isi juga username Instagram-mu supaya nama akun bisa ditampilkan di kartu platform.',
    ],
    fields: [
      { key: 'sessionid', label: 'sessionid', required: true },
      { key: 'ds_user_id', label: 'ds_user_id', required: true },
      { key: 'csrftoken', label: 'csrftoken', required: true },
      { key: 'username', label: 'username (tanpa @)', required: false, cookie: false },
    ],
  },

  async verify({ cookies, extra = {} }) {
    requireCookies(cookies, ['sessionid', 'ds_user_id', 'csrftoken']);

    // Threads memakai sesi Instagram, jadi validasinya lewat Instagram.
    const me = await sessionRequest(`https://i.instagram.com/api/v1/users/${cookies.ds_user_id}/info/`, {
      cookies,
      headers: sessionHeaders(cookies),
    });
    if (looksLoggedOut(me) || !me.data?.user) {
      throw new Error('Sesi Instagram di balik Threads sudah tidak valid. Ambil ulang cookie dari browser.');
    }

    const username = extra.username || me.data.user.username;
    const home = await sessionRequest(`${HOST}/`, { cookies, headers: { Accept: 'text/html' } });
    if (looksLoggedOut(home)) {
      throw new Error('Threads menolak sesi ini. Pastikan kamu menyalin cookie dari threads.com, bukan dari instagram.com.');
    }

    return {
      cookies: home.cookies,
      platformUserId: String(me.data.user.pk || cookies.ds_user_id),
      username,
      displayName: `@${username}`,
    };
  },

  async publish({ account, filePath, caption, log, onProgress }) {
    if (!DOC_CREATE_POST) {
      throw new Error(
        'THREADS_DOC_ID_CREATE_POST belum diisi. Ambil doc_id dari DevTools (langkahnya ada di komentar ' +
          'src/platforms/threads.js atau README), masukkan ke .env, lalu restart server.',
      );
    }

    const cookies = account.cookies;
    const size = fs.statSync(filePath).size;
    const uploadId = String(Date.now());

    const home = await sessionRequest(`${HOST}/`, { cookies, headers: { Accept: 'text/html' } });
    const tokens = extractTokens(home.text);
    if (looksLoggedOut(home)) throw new Error('Sesi Threads sudah kedaluwarsa.');

    log('Mengunggah file video…');
    const upload = await sessionRequest(`${HOST}/rupload_igvideo/${uploadId}`, {
      method: 'POST',
      cookies: home.cookies,
      headers: sessionHeaders(home.cookies, {
        'Content-Type': 'application/octet-stream',
        'X-Instagram-Rupload-Params': JSON.stringify({
          media_type: 2,
          upload_id: uploadId,
          is_sidecar: '0',
        }),
        Offset: '0',
        'X-Entity-Length': String(size),
        'X-Entity-Name': uploadId,
        'X-Entity-Type': 'video/mp4',
      }),
      body: fs.readFileSync(filePath),
    });
    if (!upload.ok) throw sessionError('Upload video ke Threads', upload);
    onProgress?.(80);

    log('Menerbitkan post…');
    const body = new URLSearchParams({
      lsd: tokens.lsd || '',
      fb_dtsg: tokens.fb_dtsg || '',
      doc_id: DOC_CREATE_POST,
      variables: JSON.stringify({
        text: caption || '',
        publish_mode: 'text_post',
        upload_id: uploadId,
        media_type: 2,
        idempotence_token: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
      }),
    });

    const post = await sessionRequest(`${HOST}/api/graphql/`, {
      method: 'POST',
      cookies: home.cookies,
      headers: sessionHeaders(home.cookies, {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-FB-LSD': tokens.lsd || '',
      }),
      body,
    });

    if (!post.ok || post.data?.errors?.length) {
      const detail = post.data?.errors?.[0]?.message || post.text?.slice(0, 200) || post.status;
      throw new Error(
        `Threads menolak (${detail}). doc_id biasanya berubah setelah Threads diperbarui — ambil doc_id baru dari DevTools.`,
      );
    }

    const payload = post.data?.data || post.data;
    const code = payload?.post_id || payload?.id || uploadId;
    return {
      id: String(code),
      url: account.username ? `https://www.threads.com/@${account.username}` : null,
      message: 'Post Threads berhasil dibuat.',
    };
  },
};
