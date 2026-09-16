import crypto from 'node:crypto';
import fs from 'node:fs';
import { EventEmitter } from 'node:events';
import { getPlatform } from './platforms/index.js';
import { getFreshConnection } from './social.js';

export const jobs = new Map();
export const bus = new EventEmitter();
bus.setMaxListeners(0);

const stamp = () => new Date().toLocaleTimeString('id-ID');

export function createJob({ user, file, caption, title, platforms, options }) {
  const id = crypto.randomUUID();
  const job = {
    id,
    userId: user.id,
    createdAt: new Date().toISOString(),
    file: { path: file.path, name: file.originalname, size: file.size },
    caption,
    title,
    options,
    status: 'running',
    targets: Object.fromEntries(
      platforms.map((p) => [
        p,
        { platform: p, status: 'queued', progress: 0, logs: [], result: null, error: null },
      ]),
    ),
  };
  jobs.set(id, job);
  return job;
}

/**
 * Semua platform diproses paralel dan masing-masing bicara langsung ke API-nya
 * sendiri. Satu platform gagal tidak menghentikan yang lain.
 */
export async function runJob(job) {
  const emit = () => bus.emit(`job:${job.id}`, job);

  await Promise.all(
    Object.keys(job.targets).map(async (platformId) => {
      const state = job.targets[platformId];
      const platform = getPlatform(platformId);
      const log = (line) => {
        state.logs.push(`${stamp()} — ${line}`);
        emit();
      };

      try {
        state.status = 'running';
        state.progress = 5;
        emit();

        const account = await getFreshConnection(job.userId, platformId);
        if (!account) throw new Error('Akun belum dihubungkan.');

        const result = await platform.publish({
          account,
          filePath: job.file.path,
          caption: job.caption,
          title: job.title,
          options: job.options || {},
          log,
          onProgress: (pct) => {
            state.progress = pct;
            emit();
          },
        });

        state.status = 'success';
        state.progress = 100;
        state.result = { id: result.id, url: result.url || null };
        log(result.message || 'Selesai.');
      } catch (err) {
        state.status = 'error';
        state.error = err.message;
        state.logs.push(`${stamp()} — ❌ ${err.message}`);
      }
      emit();
    }),
  );

  const all = Object.values(job.targets);
  job.status = all.every((t) => t.status === 'success')
    ? 'success'
    : all.every((t) => t.status === 'error')
      ? 'error'
      : 'partial';
  emit();

  // File lokal baru boleh dihapus setelah semua platform selesai mengunduhnya.
  try {
    await fs.promises.unlink(job.file.path);
    job.fileRemoved = true;
  } catch (err) {
    console.warn(`Gagal menghapus ${job.file.path}: ${err.message}`);
  }
  return job;
}
