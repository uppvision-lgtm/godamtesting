import express from 'express';
import {
  createUser,
  findUserByEmail,
  verifyPassword,
  createSession,
  destroySession,
  listUsers,
  countUsers,
  deleteUser,
} from '../db.js';
import { setSessionCookie, clearSessionCookie, requireAuth, requireAdmin } from '../session.js';

const router = express.Router();
router.use(express.json());

const publicUser = (u) => ({
  id: u.id,
  email: u.email,
  name: u.name,
  role: u.role,
  linked: Object.keys(u.connections || {}).length,
});

router.post('/api/auth/register', (req, res) => {
  const { email, password, name } = req.body || {};
  if (!email || !password) return res.status(400).json({ error: 'Email dan password wajib diisi.' });
  if (String(password).length < 8) return res.status(400).json({ error: 'Password minimal 8 karakter.' });
  if (findUserByEmail(email)) return res.status(409).json({ error: 'Email sudah terdaftar.' });

  const user = createUser({ email, password, name });
  setSessionCookie(res, createSession(user.id));
  res.json({ user: publicUser(user) });
});

router.post('/api/auth/login', (req, res) => {
  const { email, password } = req.body || {};
  const user = findUserByEmail(email);
  if (!user || !verifyPassword(password || '', user.passwordHash)) {
    return res.status(401).json({ error: 'Email atau password salah.' });
  }
  setSessionCookie(res, createSession(user.id));
  res.json({ user: publicUser(user) });
});

router.post('/api/auth/logout', (req, res) => {
  if (req.sessionToken) destroySession(req.sessionToken);
  clearSessionCookie(res);
  res.json({ ok: true });
});

router.get('/api/auth/me', (req, res) => {
  res.json({
    user: req.user ? publicUser(req.user) : null,
    // Pendaftar pertama otomatis jadi admin — UI memakai ini untuk label.
    firstUser: countUsers() === 0,
  });
});

/**
 * Hapus akun sendiri beserta seluruh token sosmed. Wajib ada dan benar-benar
 * berfungsi untuk lolos App Review — bukan sekadar janji di halaman privasi.
 */
router.delete('/api/auth/me', requireAuth, (req, res) => {
  const ok = deleteUser(req.user.id);
  clearSessionCookie(res);
  res.json({ ok });
});

router.get('/api/users', requireAuth, requireAdmin, (req, res) => {
  res.json(listUsers());
});

export default router;
