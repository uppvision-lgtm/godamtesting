import fs from 'node:fs';
import { credentials, redirectUri, env } from '../config.js';
import { request, form, pollUntil } from '../http.js';

const API = 'https://open.tiktokapis.com';
const SCOPES = ['user.info.basic', 'video.upload', 'video.publish'];

// TikTok minta chunk minimal 5 MB. File di bawah 64 MB dikirim sekali jalan.
const SINGLE_SHOT_LIMIT = 64 * 1024 * 1024;
const CHUNK = 10 * 1024 * 1024;

export default {
  id: 'tiktok',
  name: 'TikTok',
  color: '#FE2C55',
  needsPublicUrl: false,
  docs: 'https://developers.tiktok.com/doc/content-posting-api-get-started',

  authUrl(state) {
    const p = form({
      client_key: credentials.tiktok.id,
      scope: SCOPES.join(','),
      response_type: 'code',
      redirect_uri: redirectUri('tiktok'),
      state,
    });
    return `https://www.tiktok.com/v2/auth/authorize/?${p}`;
  },

  async exchange({ code }) {
    const res = await request(`${API}/v2/oauth/token/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: form({
        client_key: credentials.tiktok.id,
        client_secret: credentials.tiktok.secret,
        code: decodeURIComponent(code),
        grant_type: 'authorization_code',
        redirect_uri: redirectUri('tiktok'),
      }),
    });
    if (res.error) throw new Error(`${res.error}: ${res.error_description || ''}`);

    let displayName = 'TikTok';
    try {
      const me = await request(`${API}/v2/user/info/?fields=open_id,display_name`, {
        headers: { Authorization: `Bearer ${res.access_token}` },
      });
      displayName = me.data?.user?.display_name || displayName;
    } catch {
      /* opsional */
    }
    return {
      accessToken: res.access_token,
      refreshToken: res.refresh_token,
      expiresAt: res.expires_in ? Date.now() + res.expires_in * 1000 : null,
      openId: res.open_id,
      displayName,
    };
  },

  async refresh(account) {
    if (!account.refreshToken) return null;
    const res = await request(`${API}/v2/oauth/token/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: form({
        client_key: credentials.tiktok.id,
        client_secret: credentials.tiktok.secret,
        grant_type: 'refresh_token',
        refresh_token: account.refreshToken,
      }),
    });
    return {
      accessToken: res.access_token,
      refreshToken: res.refresh_token || account.refreshToken,
      expiresAt: res.expires_in ? Date.now() + res.expires_in * 1000 : null,
    };
  },

  async publish({ account, filePath, caption, title, options = {}, log, onProgress }) {
    const auth = { Authorization: `Bearer ${account.accessToken}` };
    const size = fs.statSync(filePath).size;
    const mode = options.tiktokMode || env('TIKTOK_POST_MODE', 'direct');
    const privacy = options.tiktokPrivacy || env('TIKTOK_PRIVACY_LEVEL', 'SELF_ONLY');

    const chunkSize = size <= SINGLE_SHOT_LIMIT ? size : CHUNK;
    const totalChunks = size <= SINGLE_SHOT_LIMIT ? 1 : Math.floor(size / CHUNK);
    const sourceInfo = { source: 'FILE_UPLOAD', video_size: size, chunk_size: chunkSize, total_chunk_count: totalChunks };

    let initUrl;
    let payload;
    if (mode === 'draft') {
      log('INIT (draft ke Inbox TikTok — nanti tinggal publish dari aplikasi TikTok)...');
      initUrl = `${API}/v2/post/publish/inbox/video/init/`;
      payload = { source_info: sourceInfo };
    } else {
      log(`INIT (direct post, privacy: ${privacy})...`);
      initUrl = `${API}/v2/post/publish/video/init/`;
      payload = {
        post_info: {
          title: (title ? `${title}\n` : '') + (caption || ''),
          privacy_level: privacy,
          disable_duet: !!options.disableDuet,
          disable_comment: !!options.disableComment,
          disable_stitch: !!options.disableStitch,
        },
        source_info: sourceInfo,
      };
    }

    const init = await request(initUrl, {
      method: 'POST',
      headers: { ...auth, 'Content-Type': 'application/json; charset=UTF-8' },
      body: JSON.stringify(payload),
    });
    if (init.error && init.error.code !== 'ok') {
      throw new Error(`${init.error.code}: ${init.error.message}`);
    }
    const { publish_id: publishId, upload_url: uploadUrl } = init.data;

    const fd = await fs.promises.open(filePath, 'r');
    try {
      for (let i = 0; i < totalChunks; i++) {
        const start = i * chunkSize;
        const end = i === totalChunks - 1 ? size - 1 : start + chunkSize - 1;
        const length = end - start + 1;
        const buf = Buffer.allocUnsafe(length);
        await fd.read(buf, 0, length, start);

        const res = await fetch(uploadUrl, {
          method: 'PUT',
          headers: {
            'Content-Type': 'video/mp4',
            'Content-Length': String(length),
            'Content-Range': `bytes ${start}-${end}/${size}`,
          },
          body: buf,
        });
        if (!res.ok && res.status !== 308) {
          throw new Error(`Upload chunk gagal: ${res.status} ${await res.text()}`);
        }
        log(`Chunk ${i + 1}/${totalChunks} terkirim`);
        onProgress?.(Math.round(((i + 1) / totalChunks) * 80));
      }
    } finally {
      await fd.close();
    }

    log('Menunggu TikTok memproses...');
    const final = await pollUntil(async () => {
      const st = await request(`${API}/v2/post/publish/status/fetch/`, {
        method: 'POST',
        headers: { ...auth, 'Content-Type': 'application/json; charset=UTF-8' },
        body: JSON.stringify({ publish_id: publishId }),
      });
      const status = st.data?.status;
      if (status === 'PUBLISH_COMPLETE' || status === 'SEND_TO_USER_INBOX') return { done: true, data: st.data };
      if (status === 'FAILED') throw new Error(`TikTok gagal: ${st.data?.fail_reason || 'FAILED'}`);
      log(`  status: ${status}`);
      return { done: false };
    }, { intervalMs: 5000 });

    return {
      id: publishId,
      url: final.data?.publicaly_available_post_id?.[0]
        ? `https://www.tiktok.com/@${account.displayName}/video/${final.data.publicaly_available_post_id[0]}`
        : null,
      message:
        mode === 'draft'
          ? 'Video terkirim ke Inbox TikTok. Buka aplikasi TikTok untuk publish.'
          : `Video diposting ke TikTok (privacy: ${privacy}).`,
    };
  },
};
