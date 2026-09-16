import crypto from 'node:crypto';
import { APP_SECRET } from './config.js';
import { readSession } from './db.js';

const COOKIE = 'sid';

/** Cookie ditandatangani supaya tidak bisa dipalsukan dari sisi browser. */
const sign = (value) => crypto.createHmac('sha256', APP_SECRET).update(value).digest('base64url');

export function setSessionCookie(res, token) {
  const value = `${token}.${sign(token)}`;
  res.setHeader('Set-Cookie', `${COOKIE}=${value}; HttpOnly; Path=/; SameSite=Lax; Max-Age=${30 * 24 * 3600}`);
}

export function clearSessionCookie(res) {
  res.setHeader('Set-Cookie', `${COOKIE}=; HttpOnly; Path=/; SameSite=Lax; Max-Age=0`);
}

function readCookie(req) {
  const raw = req.headers.cookie || '';
  const match = raw.split(';').map((c) => c.trim()).find((c) => c.startsWith(`${COOKIE}=`));
  if (!match) return null;

  const [token, signature] = decodeURIComponent(match.slice(COOKIE.length + 1)).split('.');
  if (!token || !signature) return null;

  const expected = sign(token);
  if (
    signature.length !== expected.length ||
    !crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected))
  ) {
    return null;
  }
  return token;
}

/** Menempelkan req.user kalau sesi valid. Tidak memblokir. */
export function attachUser(req, res, next) {
  const token = readCookie(req);
  req.sessionToken = token;
  req.user = token ? readSession(token) : null;
  next();
}

/** Memblokir kalau belum login. */
export function requireAuth(req, res, next) {
  if (!req.user) return res.status(401).json({ error: 'Silakan login dulu.' });
  next();
}

export function requireAdmin(req, res, next) {
  if (req.user?.role !== 'admin') return res.status(403).json({ error: 'Khusus admin.' });
  next();
}
