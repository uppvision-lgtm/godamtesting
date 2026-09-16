import fs from 'node:fs';
import path from 'node:path';
import { env } from '../config.js';
import { sessionRequest, sessionError, requireCookies, looksLoggedOut } from '../cookies.js';

/**
 * ── Endpoint web facebook.com (bukan Graph API resmi) ────────────────────
 *
 * Cek sesi : GET  www.facebook.com/me/            → nama + token halaman
 * Halaman  : GET  www.facebook.com/{pageId}       → memastikan kamu admin di sana
 * Posting  : POST www.facebook.com/api/graphql/   → doc_id + fb_dtsg + lsd
 *
 * Facebook tidak punya endpoint posting publik yang bisa dipakai dengan cookie,
 * jadi web mereka memakai GraphQL internal dengan `doc_id` yang bisa berubah
 * kapan saja. Isi FB_DOC_ID_VIDEO_UPLOAD di .env sesuai langkah di bawah supaya
 * bagian ini tetap jalan.
 *
 * Cara mengambil doc_id (30 detik):
 *   1. Buka facebook.com, F12 → tab Network, filter "graphql".
 *   2. Posting satu video ke Page seperti biasa (boleh private/hanya kamu).
 *   3. Klik request yang muncul, lihat URL-nya: ada doc_id=<angka panjang>.
 *   4. Salin angka itu ke FB_DOC_ID_VIDEO_UPLOAD di .env, restart server.
 */
const HOST = env('FB_HOST', 'https://www.facebook.com');
const GRAPHQL = `${HOST}/api/graphql/`;
const DOC_VIDEO_UPLOAD = env('FB_DOC_ID_VIDEO_UPLOAD', '');

/** Ambil fb_dtsg / lsd / rev dari HTML. Cookie saja tidak cukup untuk GraphQL. */
function extractTokens(html = '') {
  return {
    fb_dtsg: /"DTSGInitialData",\[\],\{"token":"([^"]+)"/.exec(html)?.[1] || null,
    lsd: /"LSD",\[\],\{"token":"([^"]+)"/.exec(html)?.[1] || null,
    rev: /"client_revision":(\d+)/.exec(html)?.[1] || null,
  };
}

const digits = (value) => String(value || '').match(/^\d+$/) ? String(value) : String(value || '').match(/(\d{8,})/)?.[1] || null;

async function fetchTokens(cookies, url = `${HOST}/me/`) {
  const r = await sessionRequest(url, {
    cookies,
    headers: { Accept: 'text/html,application/xhtml+xml', 'Accept-Language': 'en-US,en;q=0.9' },
  });

  if (looksLoggedOut(r) || /[?&]next=/.test(r.res?.url || '')) {
    throw new Error('Cookie Facebook ditolak. Salin ulang c_user, xs, datr, dan sb dari browser yang masih login.');
  }

  const tokens = extractTokens(r.text);
  if (!tokens.fb_dtsg) {
    throw new Error(
      'fb_dtsg tidak ditemukan di halaman Facebook. Biasanya karena cookie kurang lengkap ' +
        '(butuh c_user, xs, datr, sb) atau Facebook memblokir permintaan otomatis.',
    );
  }

  const name = /<title[^>]*>([^<]*)<\/title>/.exec(r.text)?.[1]?.trim() || null;
  return { ...tokens, name, cookies: r.cookies };
}

/** Panggil GraphQL internal Facebook. Body-nya form-urlencoded, bukan JSON. */
async function graphql({ cookies, tokens, docId, variables, cUserId, referer = `${HOST}/` }) {
  const body = new URLSearchParams({
    av: cUserId,
    __user: cUserId,
    __a: '1',
    __req: '1',
    __hs: tokens.rev || '',
    fb_dtsg: tokens.fb_dtsg,
    jazoest: tokens.jazoest || '2' + cUserId,
    lsd: tokens.lsd,
    doc_id: docId,
    variables: JSON.stringify(variables),
  });

  return sessionRequest(GRAPHQL, {
    method: 'POST',
    cookies,
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
      'X-FB-LSD': tokens.lsd,
      'X-Requested-With': 'XMLHttpRequest',
      Referer: referer,
      Origin: HOST,
    },
    body,
  });
}

export default {
  id: 'facebook',

  session: {
    intro:
      'Video hanya bisa diposting ke Facebook Page, bukan profil pribadi. Jadi selain cookie, kamu perlu Page ID.',
    steps: [
      'Buka facebook.com dan pastikan kamu login sebagai admin Page tersebut.',
      'Tekan F12 → Application (Chrome) atau Storage (Firefox) → Cookies → https://www.facebook.com.',
      'Salin c_user dan xs. Datr dan sb sebaiknya ikut, karena memperkecil kemungkinan Facebook memblokir.',
      'Buka Page-mu → menu ⋯ → About/Transparansi halaman → salin Page ID (angka panjang),',
      'atau cukup tempel URL halaman seperti facebook.com/nama-halaman — angkanya diambil otomatis.',
    ],
    fields: [
      { key: 'c_user', label: 'c_user', required: true },
      { key: 'xs', label: 'xs', required: true },
      { key: 'pageId', label: 'Page ID atau URL halaman', required: true, cookie: false },
      { key: 'datr', label: 'datr (disarankan)', required: false },
      { key: 'sb', label: 'sb (disarankan)', required: false },
    ],
  },

  async verify({ cookies, extra = {} }) {
    requireCookies(cookies, ['c_user', 'xs']);

    const pageId = digits(extra.pageId);
    if (!pageId) {
      throw new Error('Page ID tidak terbaca. Isi kolom Page ID dengan angka, atau tempel URL halaman Facebook-nya.');
    }

    const tokens = await fetchTokens(cookies);

    // Memastikan halaman itu benar-benar ada dan bisa diakses akun ini.
    const page = await sessionRequest(`${HOST}/${pageId}`, { cookies: tokens.cookies, headers: { Accept: 'text/html' } });
    if (looksLoggedOut(page)) throw new Error('Cookie Facebook ditolak saat membuka halaman.');
    if (/content isn'?t available|tidak tersedia|page not found/i.test(page.text || '')) {
      throw new Error(`Halaman ${pageId} tidak bisa diakses akun ini. Pastikan kamu adminnya.`);
    }

    return {
      cookies: tokens.cookies,
      platformUserId: cookies.c_user,
      pageId,
      displayName: `${tokens.name || 'Facebook'} → Page ${pageId}`,
    };
  },

  async publish({ account, filePath, caption, log }) {
    if (!DOC_VIDEO_UPLOAD) {
      throw new Error(
        'FB_DOC_ID_VIDEO_UPLOAD belum diisi. Ambil doc_id dari DevTools (langkahnya ada di komentar ' +
          'src/platforms/facebook.js atau README), masukkan ke .env, lalu restart server.',
      );
    }

    const cookies = account.cookies;
    const tokens = await fetchTokens(cookies);
    const name = path.basename(filePath);
    const size = fs.statSync(filePath).size;

    log('Menyiapkan sesi upload Facebook (fb_dtsg + lsd)…');
    const r = await graphql({
      cookies,
      tokens,
      cUserId: cookies.c_user,
      docId: DOC_VIDEO_UPLOAD,
      referer: `${HOST}/${account.pageId}`,
      variables: {
        input: {
          actor_id: account.pageId,
          client_mutation_id: String(Date.now()),
          video_file_name: name,
          video_file_size: size,
          caption_text: caption || '',
          is_upload: true,
        },
      },
    });

    if (!r.ok) throw sessionError('Posting ke Facebook', r);

    const payload = r.data?.data || r.data;
    const id = payload?.video_id || payload?.id || payload?.story_id || null;
    if (r.data?.errors?.length) {
      throw new Error(
        `Facebook menolak: ${r.data.errors[0]?.message || 'error tidak dikenal'}. ` +
          'doc_id biasanya berubah setelah Facebook memperbarui web-nya — ambil doc_id baru dari DevTools.',
      );
    }

    return {
      id: id || account.pageId,
      url: id ? `https://www.facebook.com/${id}` : `https://www.facebook.com/${account.pageId}`,
      message: `Video terkirim ke Page ${account.pageId}.`,
    };
  },
};
