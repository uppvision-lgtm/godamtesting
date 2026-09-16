import { GRAPH, GRAPH_VERSION, credentials, redirectUri } from '../config.js';
import { request, form, pollUntil } from '../http.js';

const SCOPES = [
  'instagram_basic',
  'instagram_content_publish',
  'pages_show_list',
  'pages_read_engagement',
  'business_management',
];

export default {
  id: 'instagram',
  name: 'Instagram Reels',
  color: '#E1306C',
  needsPublicUrl: true, // Instagram menarik video dari URL, bukan menerima file.
  docs: 'https://developers.facebook.com/docs/instagram-api/guides/content-publishing',

  authUrl(state) {
    const p = form({
      client_id: credentials.instagram.id,
      redirect_uri: redirectUri('instagram'),
      state,
      response_type: 'code',
      scope: SCOPES.join(','),
    });
    return `https://www.facebook.com/${GRAPH_VERSION}/dialog/oauth?${p}`;
  },

  async exchange({ code }) {
    const short = await request(
      `${GRAPH}/oauth/access_token?${form({
        client_id: credentials.instagram.id,
        client_secret: credentials.instagram.secret,
        redirect_uri: redirectUri('instagram'),
        code,
      })}`,
    );
    const long = await request(
      `${GRAPH}/oauth/access_token?${form({
        grant_type: 'fb_exchange_token',
        client_id: credentials.instagram.id,
        client_secret: credentials.instagram.secret,
        fb_exchange_token: short.access_token,
      })}`,
    );
    const token = long.access_token || short.access_token;

    const pages = await request(
      `${GRAPH}/me/accounts?${form({
        fields: 'id,name,instagram_business_account{id,username}',
        limit: 100,
        access_token: token,
      })}`,
    );
    const targets = (pages.data || [])
      .filter((p) => p.instagram_business_account)
      .map((p) => ({
        id: p.instagram_business_account.id,
        name: `@${p.instagram_business_account.username || p.name}`,
      }));

    if (!targets.length) {
      throw new Error(
        'Tidak ditemukan akun Instagram Professional (Business/Creator) yang terhubung ke Facebook Page. ' +
          'Ubah akun IG jadi Professional lalu tautkan ke sebuah Page.',
      );
    }
    return {
      accessToken: token,
      expiresAt: long.expires_in ? Date.now() + long.expires_in * 1000 : null,
      displayName: targets[0].name,
      targets,
      selectedTargetId: targets[0].id,
    };
  },

  async publish({ account, publicUrl, caption, shareToFeed = true, log }) {
    const igId = account.selectedTargetId || account.targets?.[0]?.id;
    if (!igId) throw new Error('Belum ada akun Instagram yang dipilih.');

    log('Membuat media container (Instagram menarik video dari URL publik)...');
    const created = await request(`${GRAPH}/${igId}/media`, {
      method: 'POST',
      body: form({
        media_type: 'REELS',
        video_url: publicUrl,
        caption: caption || '',
        share_to_feed: String(shareToFeed),
        access_token: account.accessToken,
      }),
    });

    log('Menunggu Instagram selesai memproses video...');
    await pollUntil(async () => {
      const st = await request(
        `${GRAPH}/${created.id}?${form({ fields: 'status_code,status', access_token: account.accessToken })}`,
      );
      if (st.status_code === 'FINISHED') return { done: true };
      if (st.status_code === 'ERROR') throw new Error(`Instagram gagal memproses: ${st.status || 'ERROR'}`);
      log(`  status: ${st.status_code}`);
      return { done: false };
    });

    log('Mempublikasikan Reels...');
    const published = await request(`${GRAPH}/${igId}/media_publish`, {
      method: 'POST',
      body: form({ creation_id: created.id, access_token: account.accessToken }),
    });

    let permalink = null;
    try {
      const info = await request(
        `${GRAPH}/${published.id}?${form({ fields: 'permalink', access_token: account.accessToken })}`,
      );
      permalink = info.permalink;
    } catch {
      /* permalink opsional */
    }
    return { id: published.id, url: permalink, message: 'Reels berhasil dipublikasikan.' };
  },
};
