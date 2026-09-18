"""Tangga kemajuan yang sama untuk semua platform.

Angkanya perkiraan, bukan ukuran pasti - tidak ada platform yang memberi tahu
seberapa jauh unggahannya. Yang penting urutannya monoton dan jaraknya kira-
kira sebanding dengan lamanya tiap langkah: membuka komposer cepat, mengunggah
media lama, menunggu konfirmasi sedang.

100 SENGAJA tidak ada di sini. Nilai itu hanya ditulis saat targetnya benar-
benar selesai, supaya angka penuh tidak pernah berarti "hampir selesai".
"""

MULAI = (5, "menyiapkan peramban")
HALAMAN = (12, "membuka halaman")
KOMPOSER = (25, "membuka komposer")
LAMPIR = (50, "melampirkan media")
MEDIA_SIAP = (60, "media selesai diproses")
# Dua nilai untuk langkah yang sama, karena urutannya berbeda antar platform:
# Instagram melampirkan media dulu baru caption, sedangkan Threads dan X
# sebaliknya. Yang penting angkanya selalu naik dalam satu alur - laporan yang
# lebih kecil dari laporan sebelumnya akan diabaikan.
CAPTION_AWAL = (40, "mengisi caption")
CAPTION = (72, "mengisi caption")
JADWAL = (78, "memasang jadwal")
KIRIM = (85, "menekan tombol kirim")
TUNGGU = (92, "menunggu konfirmasi")
TAUTAN = (96, "mengambil tautan")
