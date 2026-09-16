import fs from 'node:fs';
import { env } from '../config.js';
import { sessionRequest, sessionError, requireCookies, looksLoggedOut } from '../cookies.js';

/**
 * ── Endpoint web tiktok.com (bukan Content Posting API resmi) ────────────
 *
 * Cek sesi : GET  www.tiktok.com/passport/web/account/info/?aid=1988
 * Upload   : GET  www.tiktok.com/api/v1/video/upload/auth/?aid=1988     → video_token
 *            POST www.tiktok.com/api/v1/video/upload/chunk/?…           → potongan file
 *            POST www.tiktok.com/api/v1/video/upload/finish/?aid=1988   → video_id
 *
 * Ini jalur yang dipakai TikTok Studio versi web. Namanya jarang berubah, tapi
 * kalau TikTok memperbaruinya cukup ganti konstanta di bawah, bukan alurnya.
 * Karena memakai cookie, batas audit Content Posting API (SELF_ONLY) tidak
 * berlaku — video terbit seperti kamu memposting manual.
 */
const API = env('TIKTOK_WEB_API', 'https://www.tiktok.com/api/v1/video/upload');
const PASSPORT = env('TIKTOK_PASSPORT', 'https://www.tiktok.com/passport/web/account/info/');
const CHUNK = 10 * 1024 * 1024; // 10 MB per potongan

const tiktokHeaders = (cookies, extra = {}) => ({
  Accept: 'application/json, text/plain, */*',
  Referer: 'https://www.tiktok.com/',
  Origin: 'https://www.tiktok.com',
  ...extra,
});

export default {
  id: 'tiktok',

  session: {
    intro:
      'TikTok butuh beberapa cookie sekaligus. Semuanya ada di domain tiktok.com dan hanya berlaku kalau kamu tidak logout.',
    steps: [
      'Buka tiktok.com dan login di browser.',
      'Tekan F12 → Application (Chrome) atau Storage (Firefox) → Cookies → https://www.tiktok.com.',
      'Salin sessionid, sid_tt, uid_tt, dan ttwid.',
      'sessionid_ss sebaiknya ikut — TikTok kadang meminta pasangan itu saat upload.',
    ],
    fields: [
      { key: 'sessionid', label: 'sessionid', required: true },
      { key: 'sid_tt', label: 'sid_tt', required: true },
      { key: 'uid_tt', label: 'uid_tt', required: true },
      { key: 'ttwid', label: 'ttwid', required: true },
      { key: 'sessionid_ss', label: 'sessionid_ss (disarankan)', required: false },
      { key: 'odin_tt', label: 'odin_tt (opsional)', required: false },
    ],
  },

  async verify({ cookies }) {
    requireCookies(cookies, ['sessionid', 'sid_tt', 'uid_tt', 'ttwid']);

    const r = await sessionRequest(`${PASSPORT}?aid=1988&cookie_enabled=true`, {
      cookies,
      headers: tiktokHeaders(cookies),
    });

    const user = r.data?.data;
    if (looksLoggedOut(r) || !user?.username) {
      throw new Error('Sesi TikTok tidak aktif. Pastikan sessionid & sid_tt disalin dari browser yang masih login.');
    }

    return {
      cookies: r.cookies,
      platformUserId: String(user.user_id || cookies.uid_tt),
      username: user.username,
      displayName: `@${user.username}`,
    };
  },

  async publish({ account, filePath, caption, options = {}, log, onProgress }) {
    const cookies = account.cookies;
    const size = fs.statSync(filePath).size;

    log('Meminta izin upload…');
    const auth = await sessionRequest(`${API}/auth/?aid=1988&cookie_enabled=true`, {
      cookies,
      headers: tiktokHeaders(cookies),
    });
    if (!auth.ok) throw sessionError('Minta izin upload TikTok', auth);

    const videoToken = auth.data?.data?.video_token || auth.data?.data?.video_id || null;
    if (!videoToken) {
      throw sessionError('TikTok tidak memberi video_token', auth);
    }

    const total = Math.max(1, Math.ceil(size / CHUNK));
    const fh = await fs.promises.open(filePath, 'r');
    try {
      for (let i = 0; i < total; i++) {
        const start = i * CHUNK;
        const end = Math.min(start + CHUNK, size) - 1;
        const length = end - start + 1;

        const buf = Buffer.allocUnsafe(length);
        await fh.read(buf, 0, length, start);

        const url =
          `${API}/chunk/?aid=1988&video_id=${encodeURIComponent(videoToken)}` +
          `&chunk_index=${i}&total_chunk_count=${total}`;

        const r = await sessionRequest(url, {
          method: 'POST',
          cookies,
          headers: tiktokHeaders(cookies, {
            'Content-Type': 'video/mp4',
            'Content-Range': `bytes ${start}-${end}/${size}`,
            Authorization: String(videoToken),
          }),
          body: buf,
        });
        if (!r.ok) throw sessionError(`Upload potongan ${i + 1}/${total}`, r);

        log(`Potongan ${i + 1}/${total} terkirim`);
        onProgress?.(Math.round(((i + 1) / total) * 80));
      }
    } finally {
      await fh.close();
    }

    log('Menutup upload…');
    const finish = await sessionRequest(`${API}/finish/?aid=1988`, {
      method: 'POST',
      cookies,
      headers: tiktokHeaders(cookies, { 'Content-Type': 'application/json' }),
      json: {
        video_id: videoToken,
        text: caption || '',
        // TikTok web memakai 0 = publik, 1 = teman, 2 = hanya saya.
        privacy_type: Number(options.tiktokPrivacy ?? env('TIKTOK_WEB_PRIVACY', '0')),
        allow_comment: true,
        allow_duet: true,
        allow_stitch: true,
      },
    });

    if (finish.data?.status_code && finish.data.status_code !== 0) {
      throw sessionError('Menutup upload TikTok', finish);
    }

    const postId = finish.data?.data?.video_id || finish.data?.data?.item_id || videoToken;
    log(postId === videoToken ? 'Upload selesai (ID post belum tersedia).' : `Post dibuat: ${postId}`);

    return {
      id: String(postId),
      url: account.username ? `https://www.tiktok.com/@${account.username}` : null,
      message: 'Video terkirim ke TikTok. Kalau belum muncul, buka aplikasi TikTok sekali untuk memastikan.',
    };
  },
};
