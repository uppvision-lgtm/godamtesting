# Folder berkas

Taruh satu video atau foto di folder ini, lalu tulis captionnya di file `.txt`
dengan nama yang sama. Sesudah itu jalankan dari **Terminal.app**:

    ./upload-ig.sh

Contoh isi folder:

    berkas/klip.mp4      <- yang diunggah
    berkas/klip.txt      <- captionnya

Kalau semua postingan mau pakai caption yang sama, cukup buat `caption.txt`
sekali; file itu dipakai untuk media apa pun yang tidak punya `.txt` sendiri.

Kalau ada lebih dari satu media, yang dipakai adalah yang paling baru diubah.
Setelah berhasil diunggah, media dan captionnya dipindahkan ke `terkirim/`
supaya tidak terposting dua kali.
