import fs from 'node:fs';
import { credentials, redirectUri } from '../config.js';
import { request, form, pollUntil, sleep } from '../http.js';

const API = 'https://api.x.com';
const SCOPES = ['tweet.read', 'tweet.write', 'users.read', 'media.write', 'offline.access'];
const CHUNK = 4 * 1024 * 1024; // 4 MB per potongan

function basicAuth() {
  return 'Basic ' + Buffer.from(`${credentials.x.id}:${credentials.x.secret}`).toString('base64');
}

export default {
  id: 'x',
  name: 'X (Twitter)',
  color: '#111111',
  needsPublicUrl: false,
  usesPkce: true,
  docs: 'https://docs.x.com/x-api/media/quickstart/media-upload-chunked',

  authUrl(state, challenge) {
    const p = form({
      response_type: 'code',
      client_id: credentials.x.id,
      redirect_uri: redirectUri('x'),
      scope: SCOPES.join(' '),
      state,
      code_challenge: challenge,
      code_challenge_method: 'S256',
    });
    return `https://x.com/i/oauth2/authorize?${p}`;
  },

  async exchange({ code, verifier }) {
    const res = await request(`${API}/2/oauth2/token`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        ...(credentials.x.secret ? { Authorization: basicAuth() } : {}),
      },
      body: form({
        code,
        grant_type: 'authorization_code',
        client_id: credentials.x.id,
        redirect_uri: redirectUri('x'),
        code_verifier: verifier,
      }),
    });

    const me = await request(`${API}/2/users/me`, {
      headers: { Authorization: `Bearer ${res.access_token}` },
    });
    return {
      accessToken: res.access_token,
      refreshToken: res.refresh_token,
      expiresAt: res.expires_in ? Date.now() + res.expires_in * 1000 : null,
      displayName: `@${me.data?.username}`,
      userId: me.data?.id,
    };
  },

  async refresh(account) {
    if (!account.refreshToken) return null;
    const res = await request(`${API}/2/oauth2/token`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        ...(credentials.x.secret ? { Authorization: basicAuth() } : {}),
      },
      body: form({
        refresh_token: account.refreshToken,
        grant_type: 'refresh_token',
        client_id: credentials.x.id,
      }),
    });
    return {
      accessToken: res.access_token,
      refreshToken: res.refresh_token || account.refreshToken,
      expiresAt: res.expires_in ? Date.now() + res.expires_in * 1000 : null,
    };
  },

  async publish({ account, filePath, caption, log, onProgress }) {
    const auth = { Authorization: `Bearer ${account.accessToken}` };
    const buf = fs.readFileSync(filePath);

    log('INIT: mendaftarkan media ke X...');
    const init = await request(`${API}/2/media/upload/initialize`, {
      method: 'POST',
      headers: { ...auth, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        media_type: 'video/mp4',
        total_bytes: buf.length,
        media_category: 'tweet_video',
      }),
    });
    const mediaId = init.data?.id || init.data?.media_id_string || init.media_id_string;
    if (!mediaId) throw new Error('X tidak mengembalikan media id.');

    const total = Math.ceil(buf.length / CHUNK);
    for (let i = 0; i < total; i++) {
      const slice = buf.subarray(i * CHUNK, Math.min((i + 1) * CHUNK, buf.length));
      const fd = new FormData();
      fd.append('segment_index', String(i));
      fd.append('media', new Blob([slice]), 'chunk');
      await request(`${API}/2/media/upload/${mediaId}/append`, { method: 'POST', headers: auth, body: fd });
      log(`APPEND: potongan ${i + 1}/${total} terkirim`);
      onProgress?.(Math.round(((i + 1) / total) * 80));
    }

    log('FINALIZE: menutup upload...');
    const fin = await request(`${API}/2/media/upload/${mediaId}/finalize`, { method: 'POST', headers: auth });

    let info = fin.data?.processing_info;
    if (info && info.state !== 'succeeded') {
      log('Menunggu X selesai transcoding...');
      await sleep((info.check_after_secs || 5) * 1000);
      await pollUntil(async () => {
        const st = await request(
          `${API}/2/media/upload?${form({ command: 'STATUS', media_id: mediaId })}`,
          { headers: auth },
        );
        const p = st.data?.processing_info;
        if (!p || p.state === 'succeeded') return { done: true };
        if (p.state === 'failed') throw new Error(`X gagal memproses video: ${p.error?.message || 'failed'}`);
        log(`  ${p.state} ${p.progress_percent ?? 0}%`);
        return { done: false };
      }, { intervalMs: 5000 });
    }

    log('Membuat post...');
    const tweet = await request(`${API}/2/tweets`, {
      method: 'POST',
      headers: { ...auth, 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: caption || '', media: { media_ids: [String(mediaId)] } }),
    });

    const id = tweet.data?.id;
    return {
      id,
      url: id ? `https://x.com/${(account.displayName || '').replace('@', '') || 'i'}/status/${id}` : null,
      message: 'Post X berhasil dibuat.',
    };
  },
};
