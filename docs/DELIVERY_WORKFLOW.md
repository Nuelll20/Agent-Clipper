# Render dan delivery Telegram

Render video dan pengiriman Telegram adalah dua hasil yang terpisah. Klip yang
sudah selesai dirender tetap berstatus `completed` ketika Telegram sedang offline,
konfigurasi salah, atau upload ditolak. Retry delivery membaca artifact yang sudah
ada dan tidak menjalankan renderer lagi.

## Menjalankan di Windows

Dari root repository:

```powershell
# Render dan langsung coba kirim seperti perilaku default sebelumnya.
& ".\.venv\Scripts\python.exe" scripts\podcast_clipper.py render `
  --plan jobs\JOB_ID\clip-plan.json

# Render lokal tanpa Telegram.
& ".\.venv\Scripts\python.exe" scripts\podcast_clipper.py render `
  --plan jobs\JOB_ID\clip-plan.json --no-send

# Kirim semua render completed, termasuk retry setelah kegagalan Telegram.
& ".\.venv\Scripts\python.exe" scripts\podcast_clipper.py deliver `
  --manifest jobs\JOB_ID\render-manifest.json

# Kirim satu klip saja. --clip dapat dipakai lebih dari sekali.
& ".\.venv\Scripts\python.exe" scripts\podcast_clipper.py deliver `
  --manifest jobs\JOB_ID\render-manifest.json --clip clip-01
```

Perintah `render` mengembalikan exit code 1 jika render atau delivery gagal agar
otomasi mengetahui bahwa batch belum sepenuhnya selesai. Periksa manifest untuk
membedakan keduanya: kegagalan Telegram tidak mengubah `status: completed` milik
render. Jalankan `deliver` setelah koneksi atau konfigurasi diperbaiki.

## Status pada manifest

Setiap entri klip selesai memiliki object `delivery`:

```json
{
  "status": "completed",
  "clip_id": "clip-01",
  "output_video": "D:\\Hermes\\video-agent\\jobs\\demo\\clip-01-final.mp4",
  "delivery": {
    "delivery_id": "delivery_...",
    "channel": "telegram",
    "status": "failed",
    "attempts": 1,
    "last_attempt_at": "2026-10-06T00:00:00+00:00",
    "sent_at": null,
    "error": "..."
  }
}
```

Status delivery adalah:

| Status | Arti |
|---|---|
| `not_requested` | Render memakai `--no-send`; artifact boleh dikirim kemudian |
| `pending` | Pengiriman belum dimulai atau manifest lama dinormalisasi |
| `sending` | Attempt dicatat sebelum subprocess Telegram dijalankan |
| `sent` | Telegram mengembalikan identitas pesan dan manifest sudah diperbarui |
| `failed` | Delivery gagal; render tetap valid dan dapat dicoba ulang |

Path artifact baru ditulis absolut. Manifest lama dengan path relatif diselesaikan
terhadap folder tempat `render-manifest.json` berada.

## Pencegahan duplikat dan `--force`

`delivery_id` stabil dikirim ke database approval sebagai kunci unik. Menjalankan
`deliver` lagi untuk artifact berstatus `sent` akan melewatinya. Database approval
lama dimigrasikan otomatis dengan kolom dan unique index baru.

Telegram Bot API tidak menyediakan idempotency key untuk `sendVideo`. Karena itu,
jika koneksi terputus setelah request mulai dikirim atau proses mati sebelum
identitas pesan tersimpan, hasilnya dianggap belum pasti. Retry otomatis diblokir:

1. periksa chat Telegram;
2. jika video sudah ada, jangan kirim ulang;
3. jika video benar-benar belum ada, ulangi dengan `--force`.

```powershell
& ".\.venv\Scripts\python.exe" scripts\podcast_clipper.py deliver `
  --manifest jobs\JOB_ID\render-manifest.json --clip clip-01 --force
```

`--force` sengaja melewati perlindungan idempotensi dan dapat membuat duplikat.

## Dependensi Python

Dependensi langsung produksi dan tes dicatat pada `requirements.txt` dan
`requirements-dev.txt`. Bootstrap environment Python 3.11:

```powershell
uv venv --python 3.11 .venv
uv pip install --python ".\.venv\Scripts\python.exe" -r requirements-dev.txt
```

FFmpeg/FFprobe dan dependensi Node pada `motioncraft-renderer/package-lock.json`
tetap dikelola di luar manifest Python.

## Batas verifikasi

Tes otomatis memakai mock upload dan mock render. Self-test Telegram bersifat
offline. Tidak ada pesan Telegram live atau render video penuh yang dijalankan
oleh tes ini. Listener approval dan callback tetap menggunakan alur existing.
