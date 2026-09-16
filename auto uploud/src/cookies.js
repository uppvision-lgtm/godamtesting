/**
 * Cookie jar + fetch yang menyerupai browser.
 *
 * Aplikasi ini tidak lagi memakai OAuth. Pengguna menempelkan cookie login
 * miliknya sendiri, dan cookie itulah yang dikirim apa adanya ke platform.
 * Karena itu setiap request harus terlihat seperti request browser sungguhan:
 * User-Agent, Accept-Language, Referer, dan seterusnya.
 *
 * Set-Cookie dari response ikut ditelan ke jar supaya kalau platform memutar
 * (rotate) cookie di tengah proses — misalnya csrftoken Facebook atau ttwid
 * TikTok — request berikutnya sudah memakai nilai baru.
 */

export const UA =
  process.env.USER_AGENT ||
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

/** { sessionid: 'abc', csrftoken: 'xyz' } → "sessionid=abc; csrftoken=xyz" */
export function cookieHeader(cookies = {}) {
  return Object.entries(cookies)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${k}=${v}`)
    .join('; ');
}

/** Telan Set-Cookie dari sebuah response ke dalam jar (objek biasa). */
export function mergeSetCookie(jar, res) {
  const lines =
    typeof res.headers.getSetCookie === 'function'
      ? res.headers.getSetCookie()
      : [res.headers.get('set-cookie')].filter(Boolean);

  for (const line of lines) {
    const pair = String(line).split(';')[0];
    const eq = pair.indexOf('=');
    if (eq < 1) continue;

    const name = pair.slice(0, eq).trim();
    const value = pair.slice(eq + 1).trim();
    if (name) value === '' ? delete jar[name] : (jar[name] = value);
  }
  return jar;
}

/**
 * Request dengan cookie milik pengguna.
 *
 * @param {string} url
 * @param {object} opts
 * @param {object} opts.cookies  isi jar (akan disalin, tidak diubah di tempat)
 * @param {object} opts.headers  header tambahan
 * @param {*}      opts.body     body mentah (Buffer/FormData/string)
 * @param {object} opts.json     body JSON (otomatis di-stringify)
 * @returns {Promise<{res:Response, ok:boolean, status:number, text:string, data:any, cookies:object}>}
 */
export async function sessionRequest(url, opts = {}) {
  const { cookies = {}, method = 'GET', headers = {}, body, json, redirect = 'follow' } = opts;

  const jar = { ...cookies };
  const hasCookies = Object.keys(jar).length > 0;

  const res = await fetch(url, {
    method,
    redirect,
    headers: {
      'User-Agent': UA,
      'Accept-Language': 'en-US,en;q=0.9',
      Accept: '*/*',
      ...(hasCookies ? { Cookie: cookieHeader(jar) } : {}),
      ...(json !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...headers,
    },
    body: json !== undefined ? JSON.stringify(json) : body,
  });

  mergeSetCookie(jar, res);

  const text = await res.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }

  return { res, ok: res.ok, status: res.status, text, data, cookies: jar };
}

/**
 * Error yang isinya enak dibaca di log UI. Platform web hampir selalu
 * mengembalikan 200 + pesan error di dalam body, jadi pesan body lebih penting
 * daripada status HTTP-nya.
 */
export function sessionError(step, r) {
  const detail =
    r?.data?.message ||
    r?.data?.error_description ||
    r?.data?.error?.message ||
    r?.data?.msg ||
    r?.data?.status_msg ||
    (r?.text || '').replace(/\s+/g, ' ').slice(0, 300) ||
    r?.status;

  const err = new Error(`${step} gagal${r?.status ? ` (HTTP ${r.status})` : ''}: ${detail}`);
  err.status = r?.status;
  return err;
}

/**
 * Sesi yang tidak valid hampir selalu berbentuk redirect ke halaman login atau
 * body berisi "login"/"not_logged_in". Dipakai oleh verify() tiap platform.
 */
export function looksLoggedOut(r) {
  const url = r?.res?.url || '';
  const body = (r?.text || '').toLowerCase();
  return (
    /\/login|\/signin|accounts\.google\.com\/ServiceLogin/i.test(url) ||
    body.includes('user_not_login') ||
    body.includes('not_logged_in') ||
    body.includes('login_required') ||
    body.includes('please log in')
  );
}

/** Cookie wajib ada — dipakai sebelum request pertama supaya errornya jelas. */
export function requireCookies(cookies, keys) {
  const missing = keys.filter((k) => !cookies?.[k]);
  if (missing.length) {
    throw new Error(`Cookie wajib belum diisi: ${missing.join(', ')}.`);
  }
}
