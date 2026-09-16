import fs from 'node:fs';
import express from 'express';
import multer from 'multer';
import { PATHS, PLATFORM_IDS } from '../config.js';
import { createJob, runJob, jobs, bus } from '../queue.js';
import { requireAuth } from '../session.js';
import { listConnections } from '../db.js';

const router = express.Router();
fs.mkdirSync(PATHS.uploads, { recursive: true });

const upload = multer({
  storage: multer.diskStorage({
    destination: PATHS.uploads,
    filename: (req, file, cb) => cb(null, `${Date.now()}-${file.originalname.replace(/[^\w.-]/g, '_')}`),
  }),
  limits: { fileSize: 4 * 1024 * 1024 * 1024 }, // 4 GB
  fileFilter: (req, file, cb) =>
    file.mimetype.startsWith('video/') ? cb(null, true) : cb(new Error('File harus berupa video.')),
});

router.post('/api/upload', requireAuth, upload.single('video'), async (req, res, next) => {
  try {
    if (!req.file) return res.status(400).json({ error: 'Video belum dipilih.' });

    let platforms = [];
    try {
      platforms = JSON.parse(req.body.platforms || '[]').filter((p) => PLATFORM_IDS.includes(p));
    } catch {
      return res.status(400).json({ error: 'Daftar platform tidak valid.' });
    }
    if (!platforms.length) return res.status(400).json({ error: 'Pilih minimal satu platform.' });

    let options = {};
    try {
      options = JSON.parse(req.body.options || '{}');
    } catch {
      /* pakai default */
    }

    // Tolak lebih awal kalau ada platform yang belum ditautkan pengguna ini.
    const connected = Object.keys(listConnections(req.user.id));
    const missing = platforms.filter((p) => !connected.includes(p));
    if (missing.length) {
      return res.status(400).json({ error: `Belum tersambung: ${missing.join(', ')}.` });
    }

    const job = createJob({
      user: req.user,
      file: req.file,
      caption: req.body.caption || '',
      title: req.body.title || '',
      platforms,
      options,
    });

    res.json({ jobId: job.id });
    runJob(job).catch((err) => console.error('Job error:', err));
  } catch (err) {
    next(err);
  }
});

/** Progres upload secara live (Server-Sent Events). */
router.get('/api/jobs/:id/stream', requireAuth, (req, res) => {
  const job = jobs.get(req.params.id);
  if (!job || job.userId !== req.user.id) return res.status(404).end();

  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    Connection: 'keep-alive',
  });
  const send = (j) => res.write(`data: ${JSON.stringify(j)}\n\n`);
  send(job);

  const listener = (j) => send(j);
  bus.on(`job:${job.id}`, listener);
  const ping = setInterval(() => res.write(': ping\n\n'), 15000);

  req.on('close', () => {
    clearInterval(ping);
    bus.off(`job:${job.id}`, listener);
  });
});

/** Riwayat upload milik pengguna ini (selama server belum direstart). */
router.get('/api/jobs', requireAuth, (req, res) => {
  const mine = [...jobs.values()]
    .filter((j) => j.userId === req.user.id)
    .sort((a, b) => b.createdAt.localeCompare(a.createdAt))
    .slice(0, 20);
  res.json(mine);
});

export default router;
