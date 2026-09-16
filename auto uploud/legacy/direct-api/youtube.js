import fs from 'node:fs';
import { credentials, redirectUri } from '../config.js';
import { request, form } from '../http.js';

const SCOPES = [
  'https://www.googleapis.com/auth/youtube.upload',
  'https://www.googleapis.com/auth/youtube.readonly',
];

export default {
  id: 'youtube',
  name: 'YouTube Shorts',
  color: '#FF0000',
  needsPublicUrl: false,
  docs: 'https://developers.google.com/youtube/v3/guides/uploading_a_video',

  authUrl(state) {
    const p = form({
      client_id: credentials.youtube.id,
      redirect_uri: redirectUri('youtube'),
      response_type: 'code',
      scope: SCOPES.join(' '),
      access_type: 'offline',
      prompt: 'consent',
      state,
    });
    return `https://accounts.google.com/o/oauth2/v2/auth?${p}`;
  },

  async exchange({ code }) {
    const res = await request('https://oauth2.googleapis.com/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: form({
        code,
        client_id: credentials.youtube.id,
        client_secret: credentials.youtube.secret,
        redirect_uri: redirectUri('youtube'),
        grant_type: 'authorization_code',
      }),
    });

    let displayName = 'YouTube';
    try {
      const ch = await request(
        'https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true',
        { headers: { Authorization: `Bearer ${res.access_token}` } },
      );
      displayName = ch.items?.[0]?.snippet?.title || displayName;
    } catch {
      /* opsional */
    }
    return {
      accessToken: res.access_token,
      refreshToken: res.refresh_token,
      expiresAt: res.expires_in ? Date.now() + res.expires_in * 1000 : null,
      displayName,
    };
  },

  async refresh(account) {
    if (!account.refreshToken) return null;
    const res = await request('https://oauth2.googleapis.com/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: form({
        refresh_token: account.refreshToken,
        client_id: credentials.youtube.id,
        client_secret: credentials.youtube.secret,
        grant_type: 'refresh_token',
      }),
    });
    return {
      accessToken: res.access_token,
      refreshToken: account.refreshToken,
      expiresAt: res.expires_in ? Date.now() + res.expires_in * 1000 : null,
    };
  },

  async publish({ account, filePath, caption, title, options = {}, log }) {
    const size = fs.statSync(filePath).size;

    log('Memulai resumable upload...');
    const startRes = await fetch(
      'https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status',
      {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${account.accessToken}`,
          'Content-Type': 'application/json; charset=UTF-8',
          'X-Upload-Content-Length': String(size),
          'X-Upload-Content-Type': 'video/*',
        },
        body: JSON.stringify({
          snippet: {
            title: (title || caption || 'Video').slice(0, 95),
            description: caption || '',
            categoryId: '22',
          },
          status: {
            privacyStatus: options.youtubePrivacy || 'private',
            selfDeclaredMadeForKids: false,
          },
        }),
      },
    );
    if (!startRes.ok) throw new Error(`Gagal memulai upload: ${startRes.status} ${await startRes.text()}`);
    const uploadUrl = startRes.headers.get('location');
    if (!uploadUrl) throw new Error('YouTube tidak memberikan URL upload.');

    log(`Mengunggah ${(size / 1024 / 1024).toFixed(1)} MB...`);
    const res = await fetch(uploadUrl, {
      method: 'PUT',
      headers: { 'Content-Type': 'video/*', 'Content-Length': String(size) },
      body: fs.readFileSync(filePath),
    });
    if (!res.ok) throw new Error(`Upload gagal: ${res.status} ${await res.text()}`);
    const video = await res.json();

    return {
      id: video.id,
      url: `https://youtu.be/${video.id}`,
      message: `Video terunggah (privacy: ${options.youtubePrivacy || 'private'}).`,
    };
  },
};
