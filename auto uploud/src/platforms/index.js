import instagram from './instagram.js';
import tiktok from './tiktok.js';
import facebook from './facebook.js';
import threads from './threads.js';
import twitter from './twitter.js';
import youtube from './youtube.js';

/** Kunci objek ini harus sama dengan id di PLATFORMS (config.js). */
export const platforms = { instagram, tiktok, facebook, threads, twitter, youtube };
export const getPlatform = (id) => platforms[id] || null;
