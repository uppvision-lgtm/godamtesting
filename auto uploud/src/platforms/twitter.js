import fs from 'node:fs';
import { env } from '../config.js';
import { sessionRequest, sessionError, requireCookies, looksLoggedOut } from '../cookies.js';
import { pollUntil, sleep } from '../http.js';

/**
 * ── Endpoint web x.com (bukan API resmi berbayar) ────────────────────────
 *
 * Upload media   : https://upload.x.com/i/media/upload.json   (INIT/APPEND/FINALIZE/STATUS)
 * Buat post      : https://x.com/i/api/1.1/statuses/update.json
 * Cadangan       : GraphQL CreateTweet (dipakai kalau endpoint v1.1 diblokir)
 *
 * Kalau X mengganti salah satu endpoint, cukup ubah konstanta di bawah —
 * seluruh alur lainnya tidak perlu disentuh.
 */
const MEDIA_UPLOAD = env('X_MEDIA_UPLOAD_URL', 'https://upload.x.com/i/media/upload.json');
const API = env('X_WEB_API', 'https://x.com/i/api/1.1');
const CREATE_TWEET_QUERY_ID = env('X_CREATE_TWEET_QUERY_ID', 'bV2F0nwLVCXzTEIUqVFckw'); // GraphQL, cadangan saja

/** Bearer publik milik web app x.com. Bukan rahasia dan bukan milik akun siapa pun. */
const WEB_BEARER = env(
  'X_WEB_BEARER',
  'AAAAAAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA',
);

const CHUNK = 4 * 1024 * 1024; // 4 MB per potongan

/** Semua request x.com butuh kombinasi ini; tanpa x-csrf-token selalu 403. */
const apiHeaders = (cookies, extra = {}) => ({
  Authorization: `Bearer ${WEB_BEARER}`,
  'x-csrf-token': cookies.ct0,
  'x-twitter-auth-type': 'OAuth2Session',
  'x-twitter-active-user': 'yes',
  'x-twitter-client-language': 'en',
  ...extra,
});

const formBody = (obj) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(obj)) if (v !== undefined) p.append(k, String(v));
  return p;
};

export default {
  id: 'twitter',

  session: {
    intro: 'X memakai dua cookie sekaligus. Keduanya ada di domain x.com.',
    steps: [
      'Buka x.com dan pastikan kamu sudah login.',
      'Tekan F12 → tab Application (Chrome) atau Storage (Firefox) → Cookies → https://x.com.',
      'Salin nilai auth_token dan ct0 ke kolom di bawah.',
      'Jangan logout dari browser itu, karena logout akan membatalkan cookie yang sama.',
    ],
    fields: [
      { key: 'auth_token', label: 'auth_token', required: true },
      { key: 'ct0', label: 'ct0', required: true },
    ],
  },

  async verify({ cookies }) {
    requireCookies(cookies, ['auth_token', 'ct0']);

    const r = await sessionRequest(`${API}/account/verify_credentials.json?include_entities=false&skip_status=true`, {
      cookies,
      headers: apiHeaders(cookies, { Referer: 'https://x.com/home' }),
    });

    if (r.status === 401 || r.status === 403 || looksLoggedOut(r)) {
      throw new Error(
        'Cookie X ditolak. Biasanya karena auth_token sudah kedaluwarsa atau kamu sudah logout di browser.',
      );
    }
    if (!r.ok || !r.data?.screen_name) throw sessionError('Verifikasi X', r);

    return {
      cookies: r.cookies,
      platformUserId: r.data.id_str,
      username: r.data.screen_name,
      displayName: `@${r.data.screen_name}`,
    };
  },

  async publish({ account, filePath, caption, log, onProgress }) {
    const cookies = account.cookies;
    const size = fs.statSync(filePath).size;

    log('INIT: mendaftarkan media…');
    const init = await sessionRequest(MEDIA_UPLOAD, {
      method: 'POST',
      cookies,
      headers: apiHeaders(cookies, {
        'Content-Type': 'application/x-www-form-urlencoded',
        Referer: 'https://x.com/compose/post',
      }),
      body: formBody({
        command: 'INIT',
        media_type: 'video/mp4',
        total_bytes: size,
        media_category: 'tweet_video',
      }),
    });
    const mediaId = init.data?.media_id_string || init.data?.media_id;
    if (!mediaId) throw sessionError('INIT media X', init);

    const buf = fs.readFileSync(filePath);
    const total = Math.max(1, Math.ceil(buf.length / CHUNK));
    for (let i = 0; i < total; i++) {
      const slice = buf.subarray(i * CHUNK, Math.min((i + 1) * CHUNK, buf.length));
      const fd = new FormData();
      fd.append('command', 'APPEND');
      fd.append('media_id', String(mediaId));
      fd.append('segment_index', String(i));
      fd.append('media', new Blob([slice]), 'chunk');

      const r = await sessionRequest(MEDIA_UPLOAD, {
        method: 'POST',
        cookies,
        headers: apiHeaders(cookies, { Referer: 'https://x.com/compose/post' }),
        body: fd,
      });
      if (r.status >= 400) throw sessionError(`APPEND potongan ${i + 1}`, r);

      log(`APPEND: potongan ${i + 1}/${total}`);
      onProgress?.(Math.round(((i + 1) / total) * 80));
    }

    log('FINALIZE: menutup upload…');
    const fin = await sessionRequest(MEDIA_UPLOAD, {
      method: 'POST',
      cookies,
      headers: apiHeaders(cookies, {
        'Content-Type': 'application/x-www-form-urlencoded',
        Referer: 'https://x.com/compose/post',
      }),
      body: formBody({ command: 'FINALIZE', media_id: mediaId }),
    });
    if (fin.status >= 400) throw sessionError('FINALIZE media X', fin);

    const info = fin.data?.processing_info;
    if (info && info.state !== 'succeeded') {
      log('Menunggu X selesai transcoding…');
      await sleep((info.check_after_secs || 5) * 1000);
      await pollUntil(
        async () => {
          const st = await sessionRequest(`${MEDIA_UPLOAD}?${formBody({ command: 'STATUS', media_id: mediaId })}`, {
            cookies,
            headers: apiHeaders(cookies),
          });
          const p = st.data?.processing_info;
          if (!p || p.state === 'succeeded') return { done: true };
          if (p.state === 'failed') throw new Error(`X gagal memproses video: ${p.error?.message || 'failed'}`);
          log(`  ${p.state} ${p.progress_percent ?? 0}%`);
          return { done: false };
        },
        { intervalMs: 5000 },
      );
    }

    log('Membuat post…');
    const id = await createPost({ cookies, caption, mediaId, log });
    return {
      id,
      url: id ? `https://x.com/${account.username || 'i'}/status/${id}` : null,
      message: 'Post X berhasil dibuat.',
    };
  },
};

/** v1.1 lebih sederhana; kalau diblokir, jatuh ke GraphQL CreateTweet. */
async function createPost({ cookies, caption, mediaId, log }) {
  const headers = apiHeaders(cookies, {
    'Content-Type': 'application/x-www-form-urlencoded',
    Referer: 'https://x.com/compose/post',
  });

  const r = await sessionRequest(`${API}/statuses/update.json`, {
    method: 'POST',
    cookies,
    headers,
    body: formBody({ status: caption || '', media_ids: String(mediaId) }),
  });
  if (r.ok && (r.data?.id_str || r.data?.id)) return r.data.id_str || r.data.id;

  log('Endpoint v1.1 ditolak, mencoba jalur GraphQL…');
  const gql = await sessionRequest(`https://x.com/i/api/graphql/${CREATE_TWEET_QUERY_ID}/CreateTweet`, {
    method: 'POST',
    cookies,
    headers: apiHeaders(cookies, { 'Content-Type': 'application/json', Referer: 'https://x.com/compose/post' }),
    json: {
      variables: {
        tweet_text: caption || '',
        dark_request: false,
        media: { media_entities: [{ media_id: String(mediaId), tagged_users: [] }], possibly_sensitive: false },
        semantic_annotation_ids: [],
      },
      features: {
        communities_web_enable_tweet_community_results_fetch: true,
        responsive_web_graphql_timeline_navigation_enabled: true,
        longform_notetweets_consumption_enabled: true,
        view_counts_everywhere_api_enabled: true,
        rweb_video_screen_enabled: false,
      },
      queryId: CREATE_TWEET_QUERY_ID,
    },
  });

  const id = gql.data?.data?.create_tweet?.tweet_results?.result?.rest_id;
  if (!id) throw sessionError('Membuat post X', gql);
  return id;
}
