import fs from 'node:fs';
import crypto from 'node:crypto';
import { PATHS } from './config.js';

/**
 * Database JSON sederhana: { users: [], sessions: {} }.
 * Cukup untuk localhost dan tim kecil. Kalau nanti dideploy dan penggunanya
 * banyak, ganti bagian ini saja dengan SQLite/Postgres — sisanya tidak berubah.
 */
const empty = { users: [], sessions: {}, deletions: {} };

function read() {
  try {
    return { ...empty, ...JSON.parse(fs.readFileSync(PATHS.db, 'utf8')) };
  } catch {
    return structuredClone(empty);
  }
}

function write(data) {
  fs.mkdirSync(PATHS.data, { recursive: true });
  fs.writeFileSync(PATHS.db, JSON.stringify(data, null, 2));
}

/* ── Password ──────────────────────────────────────────────────────────── */

export function hashPassword(password) {
  const salt = crypto.randomBytes(16).toString('hex');
  const hash = crypto.scryptSync(password, salt, 64).toString('hex');
  return `${salt}:${hash}`;
}

export function verifyPassword(password, stored) {
  const [salt, hash] = String(stored).split(':');
  if (!salt || !hash) return false;
  const attempt = crypto.scryptSync(password, salt, 64);
  const known = Buffer.from(hash, 'hex');
  return attempt.length === known.length && crypto.timingSafeEqual(attempt, known);
}

/* ── Users ─────────────────────────────────────────────────────────────── */

export function findUserByEmail(email) {
  return read().users.find((u) => u.email === String(email).toLowerCase().trim()) || null;
}

export function findUserById(id) {
  return read().users.find((u) => u.id === id) || null;
}

export function countUsers() {
  return read().users.length;
}

export function listUsers() {
  return read().users.map(({ passwordHash, connections, ...safe }) => ({
    ...safe,
    connected: Object.keys(connections || {}),
  }));
}

export function createUser({ email, password, name }) {
  const db = read();
  const user = {
    id: crypto.randomUUID(),
    email: String(email).toLowerCase().trim(),
    name: name || String(email).split('@')[0],
    passwordHash: hashPassword(password),
    // Pengguna pertama otomatis jadi admin.
    role: db.users.length === 0 ? 'admin' : 'member',
    // Token OAuth per platform: { instagram: {...}, tiktok: {...}, ... }
    connections: {},
    createdAt: new Date().toISOString(),
  };
  db.users.push(user);
  write(db);
  return user;
}

export function updateUser(id, patch) {
  const db = read();
  const user = db.users.find((u) => u.id === id);
  if (!user) return null;
  Object.assign(user, patch);
  write(db);
  return user;
}

/* ── Sessions ──────────────────────────────────────────────────────────── */

const SESSION_TTL = 30 * 24 * 60 * 60 * 1000; // 30 hari

export function createSession(userId) {
  const db = read();
  const token = crypto.randomBytes(32).toString('hex');
  db.sessions[token] = { userId, expiresAt: Date.now() + SESSION_TTL };
  write(db);
  return token;
}

export function readSession(token) {
  if (!token) return null;
  const db = read();
  const s = db.sessions[token];
  if (!s) return null;
  if (s.expiresAt < Date.now()) {
    delete db.sessions[token];
    write(db);
    return null;
  }
  return db.users.find((u) => u.id === s.userId) || null;
}

export function destroySession(token) {
  const db = read();
  delete db.sessions[token];
  write(db);
}

/* ── Koneksi sosmed per pengguna ───────────────────────────────────────── */

export function getConnection(userId, platformId) {
  return findUserById(userId)?.connections?.[platformId] || null;
}

export function listConnections(userId) {
  return findUserById(userId)?.connections || {};
}

/** Simpan/perbarui token satu platform milik satu pengguna. */
export function saveConnection(userId, platformId, data) {
  const db = read();
  const user = db.users.find((u) => u.id === userId);
  if (!user) return null;
  user.connections ||= {};
  user.connections[platformId] = {
    ...(user.connections[platformId] || {}),
    ...data,
    connectedAt: new Date().toISOString(),
  };
  write(db);
  return user.connections[platformId];
}

/** Cari pengguna lewat id akun platform — dipakai callback hapus data Meta. */
export function findUserByPlatformUserId(platformId, platformUserId) {
  return (
    read().users.find((u) => u.connections?.[platformId]?.platformUserId === String(platformUserId)) || null
  );
}

/** Catat permintaan hapus data supaya statusnya bisa dicek lewat kode. */
export function recordDeletion(code, info) {
  const db = read();
  db.deletions ||= {};
  db.deletions[code] = { ...info, at: new Date().toISOString() };
  write(db);
}

export function readDeletion(code) {
  return read().deletions?.[code] || null;
}

/** Hapus seluruh jejak satu pengguna: akun, sesi, dan semua token sosmed. */
export function deleteUser(userId) {
  const db = read();
  const before = db.users.length;
  db.users = db.users.filter((u) => u.id !== userId);
  for (const [token, s] of Object.entries(db.sessions)) {
    if (s.userId === userId) delete db.sessions[token];
  }
  write(db);
  return db.users.length < before;
}

export function removeConnection(userId, platformId) {
  const db = read();
  const user = db.users.find((u) => u.id === userId);
  if (!user?.connections) return;
  delete user.connections[platformId];
  write(db);
}
