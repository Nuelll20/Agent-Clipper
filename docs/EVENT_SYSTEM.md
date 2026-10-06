# Event/state foundation — checkpoint B, diperluas checkpoint D

Fondasi awal mengamati pekerjaan clipper existing. Checkpoint D menambahkan
Supervisor queue dan korelasi task/run. Dashboard dan WebSocket belum tersedia.

## Penyimpanan dan identitas

- Database default: `state/production-events.db`, terpisah dari approval Telegram.
- Override melalui `HERMES_EVENT_DB` atau `--event-db` pada subcommand terkait.
- `job_id` menghubungkan pekerjaan pengguna. `run_id` unik untuk setiap invocation
  ingest/render; menjalankan ulang job tidak menimpa riwayat sebelumnya.
- Untuk CLI langsung, `task_id` tetap sama dengan `run_id`. Invocation dari
  Supervisor menerima `task_id` durable melalui `--task-id`; bounded retry
  membuat `run_id` baru tanpa mengganti identitas task.
- `events` berisi event append-only dengan sequence global; `runs` berisi snapshot
  event terakhir. Keduanya ditulis dalam satu transaksi SQLite.
- SQLite WAL dan timeout 5 detik digunakan untuk akses antarproses. Supervisor
  menambahkan lease job/resource; perintah CLI langsung tetap melewati lock ini.
- State terminal COMPLETED/FAILED tidak dapat diubah. Retry membuat run baru.

Payload mencakup schema_version, event_id, sequence, run_id, task_id, job_id,
kind, agent_id, state, stage, message, current, total, unit, clip_id dan timestamp
UTC. `agent_id=clipper` menunjukkan pipeline nyata, bukan agent LLM terpisah.
Event tidak menyimpan raw command, credential, atau isi exception; detail error
tetap pada log developer existing.

## Makna status dan progress

Run CLI mulai pada STARTING, melaporkan WORKING pada batas tahap nyata,
kemudian COMPLETED atau FAILED. KeyboardInterrupt dicatat sebagai FAILED dengan
stage interrupted. State lain dalam schema disiapkan untuk integrasi mendatang;
tidak ada tombol pause/resume yang pura-pura menghentikan proses.

Stage yang diamati meliputi download/reuse source, probe, transcribe/reuse
transcript, penyimpanan artifact, validasi, clipping, subtitle, editorial/visual
planning, motion rendering/fallback, final render, campaign dan pengiriman review.
Perintah `deliver` membuat run `CLIP_DELIVER` terpisah sehingga retry Telegram
tidak mengubah riwayat run render.

`current/total` pada render menghitung klip berhasil diproses dari klip yang dipilih
untuk invocation ini. Ia bukan persentase frame, durasi, atau estimasi waktu.
Klip gagal tidak menambah current; perintah continue-on-error tetap berakhir FAILED
jika ada kegagalan. Pada ingest, keduanya null karena belum ada total unit terukur.

COMPLETED berarti perintah CLI selesai, bukan video disetujui atau dipublikasikan.
Status render dan delivery Telegram sudah dipisahkan. Kegagalan delivery membuat
invocation berakhir FAILED agar terlihat oleh otomasi, tetapi klip pada manifest
tetap `completed` dan dapat dikirim ulang tanpa render. Approval tetap dibaca
dengan perintah `status` existing. Ingest selesai masih membutuhkan pengisian
clip-plan.

## Menjalankan di Windows

Dari `D:\Hermes\video-agent` setelah perubahan ini diterapkan:

```powershell
# Perintah produksi existing otomatis mencatat event.
# --no-send mempertahankan render lokal tanpa pengiriman Telegram.
& ".\.venv\Scripts\python.exe" scripts\podcast_clipper.py render `
  --plan jobs\JOB_ID\clip-plan.json --no-send

# Snapshot semua attempt untuk job yang sama.
& ".\.venv\Scripts\python.exe" scripts\podcast_clipper.py runs --job JOB_ID

# Ganti RUN_ID dengan ID yang dicetak saat produksi.
& ".\.venv\Scripts\python.exe" scripts\podcast_clipper.py events --run RUN_ID

# Retry Telegram dari artifact selesai, tanpa render ulang.
& ".\.venv\Scripts\python.exe" scripts\podcast_clipper.py deliver `
  --manifest jobs\JOB_ID\render-manifest.json

# Replay sesudah sequence terakhir yang telah dibaca.
& ".\.venv\Scripts\python.exe" scripts\podcast_clipper.py events `
  --run RUN_ID --after 10 --limit 100
```

`runs` dan `events` menghasilkan JSON. Batas limit 1–1000. Database belum ada
akan dibuat sebagai database kosong. UI kelak perlu menyimpan cursor terakhir,
membaca snapshot, lalu replay berdasarkan sequence untuk reconnect.

Run dapat difilter memakai task Supervisor:

```powershell
& ".\.venv\Scripts\python.exe" scripts\podcast_clipper.py runs --task TASK_ID
```

## Kegagalan dan batas checkpoint

Jika database tidak dapat dibuka/ditulis, observer mencetak warning dan dinonaktifkan
untuk invocation tersebut; pipeline tetap berjalan dengan perilaku lamanya.
Tidak ada event COMPLETED rekaan. Snapshot bisa tertinggal; jangan menyimpulkan
worker masih hidup hanya dari state WORKING/STARTING. Mati listrik, SIGKILL,
atau penghentian paksa belum direkonsiliasi. Heartbeat/lease akan ditambahkan
bersama worker supervisor, sebelum dashboard mengklaim status live.

Task Supervisor disimpan lintas proses. Lease kedaluwarsa direkonsiliasi menjadi
`RECOVERY_REQUIRED`, bukan dilanjutkan otomatis, karena proses lama mungkin masih
hidup. Detail queue, heartbeat, retry, dan recovery ada di `SUPERVISOR.md`.
Membuka/menutup UI nanti tidak boleh mengendalikan lifetime worker.

Instrumen dipasang melalui `execute_observed()` yang dipanggil CLI main. Pemanggil
Python yang memanggil command handler secara langsung harus memakai wrapper ini
atau memasok observer sendiri. JSON lama, file output, dan behavior --no-send
dipertahankan. Detail kontrak retry ada di `DELIVERY_WORKFLOW.md`.

## Verifikasi yang diperlukan

Tes baru mencakup reopen database, cursor replay, rollback event/snapshot,
attempt terpisah, penolakan update run terminal, progress invalid, exception,
interrupt, database unavailable, mixed-result render batch, dan pembacaan CLI
dari proses berbeda. Render pada tes integrasi diisolasi dengan mock; hasilnya
tidak membuktikan kualitas output video atau kemampuan GPU.

Jalankan seluruh tes existing dan baru, lalu self-test approval offline sebelum
melanjutkan checkpoint berikutnya. Verifikasi produksi Windows tetap diperlukan
untuk font, Whisper, FFmpeg/NVENC, dan Remotion.

Hasil checkpoint pada 5 Oktober 2026: seluruh suite **29 passed, 1 skipped**
(font Windows), self-test approval **OK**, compile check dan `git diff --check`
berhasil. Pengujian menggunakan Linux Python 3.12 dengan pytest 9.1.1 dan OpenCV
headless 5.0.0.93. Tidak ada pengiriman Telegram atau publikasi selama pengujian.
