import { credentials, redirectUri } from '../config.js';
import { request, form, pollUntil } from '../http.js';

const API = 'https://graph.threads.net';
const SCOPES = ['threads_basic', 'threads_content_publish'];

export default {
  id: 'threads',
  name: 'Threads',
  color: '#000000',
  needsPublicUrl: true, // sama seperti Instagram: video ditarik dari URL publik.
  docs: 'https://developers.facebook.com/docs/threads/posts',

  authUrl(state) {
    const p = form({
      client_id: credentials.threads.id,
      redirect_uri: redirectUri('threads'),
      scope: SCOPES.join(','),
      response_type: 'code',
      state,
    });
    return `https://threads.net/oauth/authorize?${p}`;
  },

  async exchange({ code }) {
    const short = await request(`${API}/oauth/access_token`, {
      method: 'POST',
      body: form({
        client_id: credentials.threads.id,
        client_secret: credentials.threads.secret,
        grant_type: 'authorization_code',
        redirect_uri: redirectUri('threads'),
        code,
      }),
    });

    // Tukar ke long-lived token (60 hari).
    let token = short.access_token;
    let expiresAt = null;
    try {
      const long = await request(
        `${API}/access_token?${form({
          grant_type: 'th_exchange_token',
          client_secret: credentials.threads.secret,
          access_token: short.access_token,
        })}`,
      );
      token = long.access_token || token;
      expiresAt = long.expires_in ? Date.now() + long.expires_in * 1000 : null;
    } catch {
      /* kalau gagal, pakai short-lived saja */
    }

    const me = await request(`${API}/v1.0/me?${form({ fields: 'id,username', access_token: token })}`);
    return {
      accessToken: token,
      expiresAt,
      userId: me.id,
      displayName: `@${me.username}`,
    };
  },

  async refresh(account) {
    const res = await request(
      `${API}/refresh_access_token?${form({
        grant_type: 'th_refresh_token',
        access_token: account.accessToken,
      })}`,
    );
    return { accessToken: res.access_token, expiresAt: Date.now() + res.expires_in * 1000 };
  },

  async publish({ account, publicUrl, caption, log }) {
    log('Membuat container Threads...');
    const created = await request(`${API}/v1.0/${account.userId}/threads`, {
      method: 'POST',
      body: form({
        media_type: 'VIDEO',
        video_url: publicUrl,
        text: caption || '',
        access_token: account.accessToken,
      }),
    });

    log('Menunggu Threads memproses video...');
    await pollUntil(async () => {
      const st = await request(
        `${API}/v1.0/${created.id}?${form({ fields: 'status,error_message', access_token: account.accessToken })}`,
      );
      if (st.status === 'FINISHED') return { done: true };
      if (st.status === 'ERROR') throw new Error(`Threads gagal memproses: ${st.error_message || 'ERROR'}`);
      log(`  status: ${st.status}`);
      return { done: false };
    });

    log('Mempublikasikan post...');
    const published = await request(`${API}/v1.0/${account.userId}/threads_publish`, {
      method: 'POST',
      body: form({ creation_id: created.id, access_token: account.accessToken }),
    });

    let permalink = null;
    try {
      const info = await request(
        `${API}/v1.0/${published.id}?${form({ fields: 'permalink', access_token: account.accessToken })}`,
      );
      permalink = info.permalink;
    } catch {
      /* opsional */
    }
    return { id: published.id, url: permalink, message: 'Post Threads berhasil dibuat.' };
  },
};
