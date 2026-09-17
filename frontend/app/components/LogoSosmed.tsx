/**
 * Lambang enam platform sosmed, digambar sebagai SVG sebaris.
 *
 * Sengaja tidak memakai berkas gambar atau pustaka ikon: enam bentuk ini
 * kecil, tidak pernah berubah, dan menggambarnya langsung berarti tidak ada
 * permintaan jaringan tambahan sekaligus warnanya bisa ikut keadaan (redup
 * saat platform belum terhubung).
 *
 * `warna` adalah warna resmi tiap merek, dipakai halaman untuk titik status
 * dan aksen lain supaya tidak ada dua sumber kebenaran.
 */

export type PlatformId = "instagram" | "tiktok" | "youtube" | "facebook" | "threads" | "twitter";

export const WARNA: Record<PlatformId, string> = {
  instagram: "#e1306c",
  tiktok: "#25f4ee",
  youtube: "#ff0000",
  facebook: "#1877f2",
  threads: "#e8e8e8",
  twitter: "#e8e8e8",
};

type Props = { platform: PlatformId; ukuran?: number; className?: string };

export default function LogoSosmed({ platform, ukuran = 20, className }: Props) {
  const p = { width: ukuran, height: ukuran, viewBox: "0 0 24 24", className, "aria-hidden": true };

  switch (platform) {
    case "instagram":
      return (
        <svg {...p} fill="none" stroke="currentColor" strokeWidth="1.8">
          <rect x="2.5" y="2.5" width="19" height="19" rx="5.5" />
          <circle cx="12" cy="12" r="4.6" />
          <circle cx="17.6" cy="6.4" r="1.15" fill="currentColor" stroke="none" />
        </svg>
      );

    case "tiktok":
      return (
        <svg {...p} fill="currentColor">
          <path d="M16.6 2h-3.02v13.1a2.5 2.5 0 1 1-2.13-2.47v-3.06a5.53 5.53 0 1 0 5.15 5.52V8.9a6.9 6.9 0 0 0 4.1 1.33V7.2a4.07 4.07 0 0 1-4.1-4.07V2z" />
        </svg>
      );

    case "youtube":
      return (
        <svg {...p} fill="currentColor">
          <path d="M22.5 7.2a2.75 2.75 0 0 0-1.93-1.95C18.85 4.8 12 4.8 12 4.8s-6.85 0-8.57.45A2.75 2.75 0 0 0 1.5 7.2 28.8 28.8 0 0 0 1.05 12c0 1.62.15 3.23.45 4.8a2.75 2.75 0 0 0 1.93 1.95c1.72.45 8.57.45 8.57.45s6.85 0 8.57-.45a2.75 2.75 0 0 0 1.93-1.95c.3-1.57.45-3.18.45-4.8s-.15-3.23-.45-4.8z" />
          <path d="M9.9 15.3 15.6 12 9.9 8.7v6.6z" fill="#0f1210" />
        </svg>
      );

    case "facebook":
      return (
        <svg {...p} fill="currentColor">
          <path d="M22 12.06C22 6.5 17.52 2 12 2S2 6.5 2 12.06c0 5.02 3.66 9.18 8.44 9.94v-7.03H7.9v-2.91h2.54V9.85c0-2.52 1.49-3.91 3.77-3.91 1.09 0 2.24.2 2.24.2v2.46h-1.26c-1.24 0-1.63.78-1.63 1.57v1.89h2.78l-.45 2.91h-2.33v7.03C18.34 21.24 22 17.08 22 12.06z" />
        </svg>
      );

    case "threads":
      return (
        <svg {...p} fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
          <path d="M16.3 11.4c-.15-.07-.3-.14-.46-.2-.27-4.9-2.95-7.7-7.45-7.73h-.06c-2.69 0-4.93 1.15-6.31 3.24l2.02 1.39c1.03-1.56 2.64-1.9 4.29-1.9h.04c1.23 0 2.16.36 2.76 1.07.44.51.73 1.22.88 2.12a16.4 16.4 0 0 0-3.4-.16c-3.42.2-5.62 2.19-5.47 4.96.07 1.4.77 2.61 1.97 3.4 1.01.67 2.31 1 3.67.93 1.79-.1 3.2-.78 4.18-2.03.74-.95 1.21-2.18 1.42-3.72.86.52 1.5 1.2 1.85 2.02.6 1.4.64 3.68-1.23 5.54-1.63 1.63-3.6 2.34-6.57 2.36-3.3-.02-5.79-1.08-7.41-3.14C1.7 17.6 1.92 14.8 1.9 12c.02-2.8-.2-5.6 1.64-7.94"
            transform="translate(2.4 0.6) scale(0.86)" />
        </svg>
      );

    case "twitter":
      return (
        <svg {...p} fill="currentColor">
          <path d="M17.53 3h3.2l-6.99 7.99L22 21h-6.44l-5.04-6.6L4.75 21H1.54l7.48-8.55L2 3h6.6l4.56 6.03L17.53 3zm-1.12 16.08h1.77L7.68 4.82H5.78l10.63 14.26z" />
        </svg>
      );

    default:
      return null;
  }
}
