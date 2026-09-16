import fs from 'node:fs';
import path from 'node:path';
import { Readable } from 'node:stream';
import { AYRSHARE_API_KEY, AYRSHARE_DOMAIN } from '../config.js';
import { request } from '../http.js';

const API = 'https://api.ayrshare.com/api';

const auth = (profileKey) => ({
  Authorization: `Bearer ${AYRSHARE_API_KEY}`,
  ...(profileKey ? { 'Profile-Key': profileKey } : {}),
});

const json = (profileKey) => ({ ...auth(profileKey), 'Content-Type': 'application/json' });

export default {
  id: 'ayrshare',
  name: 'Ayrshare',
  configured: () => Boolean(AYRSHARE_API_KEY),

  /**
   * Satu User Profile per pengguna aplikasi kita. Profile Key-nya rahasia dan
   * hanya hidup di server — browser tidak pernah melihatnya.
   */
  async createProfile(title) {
    const res = await request(`${API}/profiles`, {
      method: 'POST',
      headers: json(),
      body: JSON.stringify({ title }),
    });
    return { profileKey: res.profileKey, refId: res.refId };
  },

  async deleteProfile(profileKey) {
    return request(`${API}/profiles`, {
      method: 'DELETE',
      headers: json(),
      body: JSON.stringify({ profileKey }),
    });
  },

  /**
   * URL halaman "Tautkan Akun" milik provider. Pengguna login ke Instagram/
   * TikTok/dll di sana, lalu kembali ke aplikasi kita. Token berlaku singkat.
   */
  async linkUrl(profileKey, { redirect, allowedSocial, expiresIn = 60 } = {}) {
    const res = await request(`${API}/profiles/generateJWT`, {
      method: 'POST',
      headers: json(),
      body: JSON.stringify({
        profileKey,
        ...(AYRSHARE_DOMAIN ? { domain: AYRSHARE_DOMAIN } : {}),
        ...(redirect ? { redirect } : {}),
        ...(allowedSocial ? { allowedSocial } : {}),
        expiresIn,
      }),
    });
    return res.url;
  },

  /** Daftar akun sosial yang sudah ditautkan pengguna ini. */
  async linkedAccounts(profileKey) {
    const res = await request(`${API}/user`, { headers: auth(profileKey) });
    const names = res.displayNames || [];
    return {
      active: res.activeSocialAccounts || [],
      details: names.map((d) => ({
        platform: d.platform,
        displayName: d.displayName || d.username || d.pageName || null,
        username: d.username || null,
        image: d.userImage || d.profileUrl || null,
      })),
    };
  },

  /**
   * Upload video ke storage provider, lalu pakai accessUrl waktu posting.
   * Inilah sebabnya tunnel (ngrok/cloudflared) tidak lagi diperlukan.
   */
  async uploadVideo({ filePath, fileName, onProgress }) {
    const ext = (path.extname(fileName || filePath) || '.mp4').slice(1).toLowerCase();
    const presigned = await request(
      `${API}/media/uploadUrl?fileName=${encodeURIComponent(fileName)}&contentType=${encodeURIComponent(ext)}`,
      { headers: auth() },
    );

    const size = fs.statSync(filePath).size;
    let sent = 0;
    const stream = fs.createReadStream(filePath);
    stream.on('data', (chunk) => {
      sent += chunk.length;
      onProgress?.(Math.round((sent / size) * 100));
    });

    const res = await fetch(presigned.uploadUrl, {
      method: 'PUT',
      headers: { 'Content-Type': presigned.contentType, 'Content-Length': String(size) },
      body: Readable.toWeb(stream),
      duplex: 'half',
    });
    if (!res.ok) throw new Error(`Upload media gagal: ${res.status} ${await res.text()}`);

    return presigned.accessUrl;
  },

  /** Kirim satu video ke banyak platform sekaligus. */
  async publish({ profileKey, platforms, caption, title, mediaUrl, options = {} }) {
    const body = {
      post: caption || '',
      platforms,
      mediaUrls: [mediaUrl],
      isVideo: true,
    };

    if (platforms.includes('youtube')) {
      body.youTubeOptions = {
        title: (title || caption || 'Video').slice(0, 95),
        visibility: options.youtubePrivacy || 'private',
      };
    }
    if (platforms.includes('tiktok')) {
      body.tikTokOptions = {
        privacyLevel: options.tiktokPrivacy || 'SELF_ONLY',
        disableComment: !!options.disableComment,
        disableDuet: !!options.disableDuet,
        disableStitch: !!options.disableStitch,
      };
    }
    if (platforms.includes('instagram')) {
      body.instagramOptions = { reels: true, shareReelsFeed: true };
    }
    if (platforms.includes('facebook')) {
      body.faceBookOptions = { ...(title ? { title } : {}) };
    }

    let res;
    try {
      res = await request(`${API}/post`, { method: 'POST', headers: json(profileKey), body: JSON.stringify(body) });
    } catch (err) {
      // Status 'error' tetap membawa rincian per platform — pakai itu kalau ada.
      if (err.body?.postIds || err.body?.errors) res = err.body;
      else throw err;
    }

    const byPlatform = {};
    for (const p of platforms) byPlatform[p] = { status: 'error', error: 'Tidak ada respons dari provider.' };
    for (const ok of res.postIds || []) {
      byPlatform[ok.platform] = { status: 'success', id: ok.id, url: ok.postUrl || null };
    }
    for (const bad of res.errors || []) {
      byPlatform[bad.platform] = { status: 'error', error: bad.message || bad.code || 'Gagal.' };
    }
    return { id: res.id, byPlatform };
  },
};
