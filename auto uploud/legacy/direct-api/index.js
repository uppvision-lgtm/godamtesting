import facebook from './facebook.js';
import instagram from './instagram.js';
import threads from './threads.js';
import x from './x.js';
import tiktok from './tiktok.js';
import youtube from './youtube.js';

export const platforms = { instagram, tiktok, facebook, threads, x, youtube };
export const platformList = Object.values(platforms);
export const getPlatform = (id) => platforms[id] || null;
