import fs from 'node:fs';
import { GRAPH, GRAPH_VERSION, credentials, redirectUri } from '../config.js';
import { request, form } from '../http.js';

const SCOPES = [
  'pages_show_list',
  'pages_read_engagement',
  'pages_manage_posts',
  'publish_video',
  'business_management',
];

export default {
  id: 'facebook',
  name: 'Facebook Page',
  color: '#1877F2',
  needsPublicUrl: false,
  docs: 'https://developers.facebook.com/docs/video-api/guides/publishing',

  authUrl(state) {
    const p = form({
      client_id: credentials.facebook.id,
      redirect_uri: redirectUri('facebook'),
      state,
      response_type: 'code',
      scope: SCOPES.join(','),
    });
    return `https://www.facebook.com/${GRAPH_VERSION}/dialog/oauth?${p}`;
  },

  async exchange({ code }) {
    const short = await request(
      `${GRAPH}/oauth/access_token?${form({
        client_id: credentials.facebook.id,
        client_secret: credentials.facebook.secret,
        redirect_uri: redirectUri('facebook'),
        code,
      })}`,
    );
    // Tukar jadi long-lived token (± 60 hari) supaya tidak sering login ulang.
    const long = await request(
      `${GRAPH}/oauth/access_token?${form({
        grant_type: 'fb_exchange_token',
        client_id: credentials.facebook.id,
        client_secret: credentials.facebook.secret,
        fb_exchange_token: short.access_token,
      })}`,
    );
    const userToken = long.access_token || short.access_token;

    const me = await request(`${GRAPH}/me?${form({ fields: 'id,name', access_token: userToken })}`);
    const pages = await request(
      `${GRAPH}/me/accounts?${form({ fields: 'id,name,access_token', limit: 100, access_token: userToken })}`,
    );

    const targets = (pages.data || []).map((p) => ({ id: p.id, name: p.name, token: p.access_token }));
    if (!targets.length) {
      throw new Error('Akun ini tidak punya Facebook Page. Video hanya bisa diposting ke Page, bukan profil pribadi.');
    }
    return {
      accessToken: userToken,
      expiresAt: long.expires_in ? Date.now() + long.expires_in * 1000 : null,
      displayName: me.name,
      targets,
      selectedTargetId: targets[0].id,
    };
  },

  async publish({ account, filePath, caption, title, log }) {
    const page = (account.targets || []).find((t) => t.id === account.selectedTargetId) || account.targets?.[0];
    if (!page) throw new Error('Belum ada Page yang dipilih.');

    log(`Mengunggah langsung ke Page "${page.name}"...`);
    const fd = new FormData();
    fd.append('access_token', page.token);
    fd.append('description', caption || '');
    if (title) fd.append('title', title);
    fd.append('source', new Blob([fs.readFileSync(filePath)]), 'video.mp4');

    const res = await request(`${GRAPH}/${page.id}/videos`, { method: 'POST', body: fd });
    return {
      id: res.id,
      url: `https://www.facebook.com/${res.id}`,
      message: 'Video terkirim ke Facebook Page.',
    };
  },
};
