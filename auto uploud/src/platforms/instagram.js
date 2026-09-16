import fs from 'node:fs';
import { env } from '../config.js';
import { sessionRequest, sessionError, requireCookies, looksLoggedOut } from '../cookies.js';
import { pollUntil } from '../http.js';

/**
 * ── Endpoint web Instagram (bukan API resmi Graph) ───────────────────────
 *
 * Cek sesi  : GET  www.instagram.com/api/v1/users/{ds_user_id}/info/
 * Upload    : POST www.instagram.com/rupload_igvideo/{upload_id}   ← file mentah
 * Terbitkan : POST www.instagram.com/api/v1/media/configure_to_reel/
 *
 * Bedanya dengan jalur Graph API: video dikirim langsung sebagai file, jadi
 * Instagram & Threads TIDAK butuh alamat publik / tunnel lagi.
 */
const WEB = env('IG_WEB_API', 'https://www.instagram.com');
const APP_ID = env('IG_APP_ID', '936619743392459'); // App ID publik web Instagram

/** Semua request web Instagram butuh X-IG-App-ID dan CSRF dari cookie. */
const igHeaders = (cookies, extra = {}) => ({
  'X-IG-App-ID': APP_ID,
  'X-CSRFToken': cookies.csrftoken,
  'X-Requested-With': 'XMLHttpRequest',
  'X-ASBD-ID': '129477',
  'X-IG-WWW-Claim': '0',
  Referer: 'https://www.instagram.com/',
  Origin: 'https://www.instagram.com',
  ...extra,
});

const formBody = (obj) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(obj)) if (v !== undefined && v !== null) p.append(k, String(v));
  return p;
};

export default {
  id: 'instagram',

  session: {
    intro:
      'Cukup tiga nilai dari instagram.com. Pastikan akunmu sudah diubah menjadi Professional (Business/Creator).',
    steps: [
      'Buka instagram.com dan login.',
      'Buka profilmu, lihat URL-nya: instagram.com/username/ — angka di belakangnya bukan yang dibutuhkan.',
      'Tekan F12 → Application (Chrome) atau Storage (Firefox) → Cookies → https://www.instagram.com.',
      'Salin sessionid (panjang, berisi %3A), ds_user_id, dan csrftoken.',
      'Jangan klik Log out di browser — itu membatalkan sessionid yang sama.',
    ],
    fields: [
      { key: 'sessionid', label: 'sessionid', required: true },
      { key: 'ds_user_id', label: 'ds_user_id', required: true },
      { key: 'csrftoken', label: 'csrftoken', required: true },
      { key: 'mid', label: 'mid (opsional)', required: false },
      { key: 'ig_did', label: 'ig_did (opsional)', required: false },
    ],
  },

  async verify({ cookies }) {
    requireCookies(cookies, ['sessionid', 'ds_user_id', 'csrftoken']);

    const r = await sessionRequest(`${WEB}/api/v1/users/${cookies.ds_user_id}/info/`, {
      cookies,
      headers: igHeaders(cookies),
    });

    if (looksLoggedOut(r) || r.data?.message === 'login_required') {
      throw new Error('Sesi Instagram sudah tidak valid. Ambil ulang cookie sessionid dari browser.');
    }
    if (!r.ok || !r.data?.user) throw sessionError('Verifikasi Instagram', r);

    const user = r.data.user;
    return {
      cookies: r.cookies,
      platformUserId: String(user.pk || cookies.ds_user_id),
      username: user.username,
      displayName: `@${user.username}`,
      isPrivate: !!user.is_private,
    };
  },

  async publish({ account, filePath, caption, log, onProgress }) {
    const cookies = account.cookies;
    const size = fs.statSync(filePath).size;
    const uploadId = String(Date.now());

    log('Mengunggah file video (rupload)…');
    const upload = await sessionRequest(`${WEB}/rupload_igvideo/${uploadId}`, {
      method: 'POST',
      cookies,
      headers: igHeaders(cookies, {
        'Content-Type': 'application/octet-stream',
        'X-Instagram-Rupload-Params': JSON.stringify({
          media_type: 2,
          upload_id: uploadId,
          upload_media_width: 1080,
          upload_media_height: 1920,
          xsharing_user_ids: '[]',
          for_direct_story: false,
          is_sidecar: '0',
        }),
        Offset: '0',
        'X-Entity-Length': String(size),
        'X-Entity-Name': uploadId,
        'X-Entity-Type': 'video/mp4',
      }),
      body: fs.readFileSync(filePath),
    });

    if (!upload.ok) throw sessionError('Upload video ke Instagram', upload);
    onProgress?.(80);

    log('Menerbitkan sebagai Reels…');
    const publish = await sessionRequest(`${WEB}/api/v1/media/configure_to_reel/`, {
      method: 'POST',
      cookies,
      headers: igHeaders(cookies, { 'Content-Type': 'application/x-www-form-urlencoded' }),
      body: formBody({
        upload_id: uploadId,
        caption: caption || '',
        share_to_feed: 'true',
        clips_share_preview_to_feed: 'true',
        igtv_share_preview_to_feed: '1',
        disable_comments: '0',
        like_and_view_counts_disabled: '0',
        audio_muted: 'false',
        is_reel_media: '1',
        source_type: '4',
        timezone_offset: String(-new Date().getTimezoneOffset() * 60),
      }),
    });

    const media = publish.data?.media;
    if (!media) throw sessionError('Menerbitkan Reels', publish);

    log('Menunggu Instagram selesai memproses…');
    const code = await waitForProcessing({ cookies, media, log });

    return {
      id: String(media.pk || code || uploadId),
      url: code ? `https://www.instagram.com/reel/${code}/` : null,
      message: 'Reels berhasil dipublikasikan.',
    };
  },
};

/**
 * configure_to_reel sering membalas lebih dulu sebelum video selesai diproses,
 * jadi kita tunggu sampai Instagram memberi tahu hasilnya.
 */
async function waitForProcessing({ cookies, media, log }) {
  const pk = media.pk;
  if (!pk) return media.code || null;

  try {
    const final = await pollUntil(
      async () => {
        const r = await sessionRequest(`${WEB}/api/v1/media/${pk}/info/`, { cookies, headers: igHeaders(cookies) });
        const item = r.data?.items?.[0];
        if (!item) return { done: false };

        // Instagram baru selesai kalau versi video dan kode postingannya sudah ada.
        if (item.video_versions?.length && item.code) return { done: true, data: item };

        log('  masih diproses Instagram…');
        return { done: false };
      },
      { intervalMs: 8000, timeoutMs: 5 * 60 * 1000 },
    );
    return final.data?.code || media.code || null;
  } catch {
    // Video tetap masuk walau status akhirnya tidak sempat terbaca.
    return media.code || null;
  }
}
