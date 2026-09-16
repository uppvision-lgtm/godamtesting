/** Helper HTTP kecil yang melempar error dengan isi body supaya mudah didebug. */
export async function request(url, options = {}) {
  const res = await fetch(url, options);
  const text = await res.text();
  let body;
  try {
    body = text ? JSON.parse(text) : {};
  } catch {
    body = { raw: text };
  }
  if (!res.ok) {
    const detail =
      body?.error?.message ||
      body?.error_description ||
      body?.error?.description ||
      body?.message ||
      body?.detail ||
      body?.raw ||
      JSON.stringify(body);
    const err = new Error(`${res.status} ${res.statusText} — ${detail}`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Polling status dengan batas waktu, dipakai IG / Threads / TikTok / X. */
export async function pollUntil(fn, { intervalMs = 4000, timeoutMs = 10 * 60 * 1000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const result = await fn();
    if (result?.done) return result;
    await sleep(intervalMs);
  }
  throw new Error('Timeout menunggu proses selesai di sisi platform.');
}

export const form = (obj) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(obj)) if (v !== undefined && v !== null) p.append(k, String(v));
  return p;
};
