import path from 'node:path';
import express from 'express';
import { PORT, BASE_URL, ROOT, APP_SECRET, PLATFORMS } from './config.js';
import { attachUser } from './session.js';
import authRoutes from './routes/auth.js';
import socialRoutes from './routes/social.js';
import legalRoutes from './routes/legal.js';
import uploadRoutes from './routes/upload.js';

const app = express();

app.use(express.static(path.join(ROOT, 'public')));
app.use(attachUser);
app.use(authRoutes);
app.use(socialRoutes);
app.use(legalRoutes);
app.use(uploadRoutes);

app.use((err, req, res, next) => {
  console.error(err.message);
  res.status(err.status && err.status < 500 ? err.status : 400).json({ error: err.message });
});

app.listen(PORT, () => {
  console.log('\n  🎬 Auto Upload Sosmed');
  console.log(`  ➜ Buka: ${BASE_URL}\n`);
  console.log('  Mode: sesi/cookie — tanpa OAuth, tanpa daftar app, tanpa tunnel.');
  console.log(`  Platform: ${PLATFORMS.map((p) => p.name).join(', ')}`);

  // Facebook & Threads memakai GraphQL internal yang doc_id-nya bisa berubah.
  const missingDocIds = [
    ['Facebook', 'FB_DOC_ID_VIDEO_UPLOAD'],
    ['Threads', 'THREADS_DOC_ID_CREATE_POST'],
  ].filter(([, key]) => !process.env[key]);
  if (missingDocIds.length) {
    console.log(
      `  ℹ ${missingDocIds.map(([name, key]) => `${name} (${key})`).join(', ')} belum diisi — ` +
        'empat platform lain sudah bisa dipakai. Lihat README §doc_id.',
    );
  }

  if (APP_SECRET.startsWith('ganti-')) console.log('  ⚠ APP_SECRET masih nilai bawaan.');
  console.log('');
});
