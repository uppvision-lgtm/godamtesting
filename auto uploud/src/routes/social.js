import crypto from 'node:crypto';
import express from 'express';
import { PLATFORMS } from '../config.js';
import { getPlatform } from '../platforms/index.js';
import { listConnections, saveConnection, removeConnection, getConnection } from '../db.js';
import { requireAuth } from '../session.js';

const router = express.Router();
router.use(express.json());

/** Pisahkan nilai yang masuk: mana cookie, mana keterangan tambahan. */
function splitFields(platform, input = {}) {
  const cookies = {};
  const extra = {};

  for (const field of platform.session.fields) {
    const value = String(input[field.key] ?? '').trim();
    if (!value) continue;
    if (field.cookie === false) extra[field.key] = value;
    else cookies[field.key] = value;
  }
  return { cookies, extra };
}

function assertRequired(platform, input = {}) {
  for (const field of platform.session.fields) {
    if (field.required && !String(input[field.key] ?? '').trim()) {
      throw new Error(`${field.label} wajib diisi.`);
    }
  }
}

/* ── Status ────────────────────────────────────────────────────────────── */

/** Status tautan tiap platform + petunjuk cara ambil cookie-nya. */
router.get('/api/social/status', requireAuth, (req, res) => {
  const connections = listConnections(req.user.id);

  res.json({
    platforms: PLATFORMS.map((p) => {
      const conn = connections[p.id];
      const platform = getPlatform(p.id);
      return {
        ...p,
        intro: platform.session.intro,
        steps: platform.session.steps,
        fields: platform.session.fields,
        connected: Boolean(conn),
        displayName: conn?.displayName || null,
        linkedAt: conn?.connectedAt || null,
        stale: conn?.stale || false,
      };
    }),
  });
});

/* ── Tautkan / lepas ───────────────────────────────────────────────────── */

/**
 * Simpan cookie milik pengguna. Cookie dicoba dulu ke platform (verify) supaya
 * pengguna langsung tahu kalau salah salin, bukan nanti saat upload.
 */
router.post('/api/social/:platform/connect', requireAuth, async (req, res) => {
  const platform = getPlatform(req.params.platform);
  if (!platform) return res.status(404).json({ error: 'Platform tidak dikenal.' });

  try {
    assertRequired(platform, req.body?.fields);
    const { cookies, extra } = splitFields(platform, req.body?.fields);

    const account = await platform.verify({ cookies, extra });
    saveConnection(req.user.id, platform.id, { ...account, mode: 'session' });

    res.json({ ok: true, displayName: account.displayName || null });
  } catch (err) {
    res.status(400).json({ error: err.message });
  }
});

/** Cek ulang sesi yang sudah tersimpan — berguna kalau upload mulai gagal. */
router.post('/api/social/:platform/check', requireAuth, async (req, res) => {
  const platform = getPlatform(req.params.platform);
  if (!platform) return res.status(404).json({ error: 'Platform tidak dikenal.' });

  const account = getConnection(req.user.id, req.params.platform);
  if (!account) return res.status(404).json({ error: 'Akun belum tersambung.' });

  try {
    const verified = await platform.verify({ cookies: account.cookies, extra: account });
    saveConnection(req.user.id, platform.id, { ...verified, mode: 'session', stale: false });
    res.json({ ok: true, displayName: verified.displayName || account.displayName || null });
  } catch (err) {
    saveConnection(req.user.id, platform.id, { stale: true });
    res.status(400).json({ error: err.message });
  }
});

router.post('/api/social/:platform/disconnect', requireAuth, (req, res) => {
  removeConnection(req.user.id, req.params.platform);
  res.json({ ok: true });
});

export default router;
