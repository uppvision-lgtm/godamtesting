# legacy/direct-api

Versi pertama aplikasi ini: integrasi **langsung** ke API tiap platform dengan
app milik sendiri (OAuth sendiri, token disimpan lokal).

Ditinggalkan karena app yang belum lolos audit terkunci:
TikTok `SELF_ONLY`, YouTube `private`, X butuh paket berbayar — dan Development
Mode Meta cuma melayani akun tester, jadi tidak bisa dipakai banyak pengguna.

Disimpan di sini kalau suatu saat mau kembali ke model hybrid
(Instagram/Facebook/Threads pakai app sendiri, sisanya lewat pihak ketiga).
Kodenya masih utuh dan pernah diuji sampai tahap job queue.
