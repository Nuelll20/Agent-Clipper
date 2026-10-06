# Supervisor durable — checkpoint D, diperluas checkpoint E

Supervisor menambahkan queue task lokal yang durable tanpa mengganti pipeline
Agent-Clipper. Adapter `CLIP_VIDEO` tetap memanggil
`scripts/podcast_clipper.py render`, sehingga caption, editorial, MotionCraft,
artifact gate, manifest, dan event existing tetap menjadi jalur produksi.
Checkpoint E menambahkan adapter `DEVELOP_STORY` yang memanggil enam role melalui
`scripts/story_studio.py`.

## Penyimpanan dan identitas

- Queue default: `state/supervisor.db`.
- Override dengan `HERMES_SUPERVISOR_DB` atau global option `--db`.
- Event produksi tetap berada di `state/production-events.db`.
- `job_id` mengelompokkan satu pekerjaan pengguna.
- `task_id` adalah unit queue durable dan tetap sama sepanjang bounded retry.
- `run_id` adalah satu invocation CLI. Beberapa run dapat merujuk satu task.

Database supervisor menyimpan snapshot `tasks`, transisi append-only
`task_events`, dan `resource_leases`. Semua perubahan task dan lease terkait
ditulis dalam satu transaksi SQLite `BEGIN IMMEDIATE`.

## State task

| State | Arti |
|---|---|
| `QUEUED` | Siap diklaim worker |
| `RUNNING` | Sedang dimiliki satu worker dengan lease aktif |
| `RETRY_WAIT` | Kegagalan dikenal; menunggu backoff dalam attempt budget |
| `REVIEW_REQUIRED` | Critic/continuity meminta keputusan atau revisi manusia; tidak auto-retry |
| `RECOVERY_REQUIRED` | Heartbeat hilang; operator harus memeriksa sebelum retry |
| `FAILED` | Gagal permanen atau attempt budget habis |
| `COMPLETED` | Adapter selesai dengan exit code 0 |

## Invariant keselamatan

1. Enqueue memiliki `idempotency_key` unik. Default key memasukkan `job_id`,
   path dan SHA-256 clip plan, pilihan klip, serta mode delivery.
2. Worker menghitung ulang SHA-256 clip plan sebelum menjalankan adapter. Plan
   yang berubah setelah enqueue diblokir dan harus diantrikan sebagai task baru.
3. Claim selalu mengambil lease `job:<job_id>` dan resource
   `clipper-render`. Dua worker tidak dapat menulis job yang sama atau memakai
   resource render yang sama secara bersamaan.
4. Worker memperbarui heartbeat sebelum lease berakhir. Update, completion,
   dan failure memerlukan `worker_id` serta token lease yang masih valid.
5. Exit code render nonzero dapat diulang dengan exponential backoff hanya
   sampai `max_attempts`.
6. Lease kedaluwarsa tidak langsung menjalankan task lagi. State berubah menjadi
   `RECOVERY_REQUIRED`; ini mencegah worker baru beradu dengan proses lama yang
   status hidup/matinya belum pasti.
7. Adapter `CLIP_VIDEO` selalu menambahkan `--no-send`. Retry Supervisor tidak
   mengirim Telegram atau mempublikasikan video. Delivery tetap memakai CLI
   `deliver` setelah artifact render dipastikan selesai.
8. `DEVELOP_STORY` menghitung ulang fingerprint brief, memakai `task_id` sebagai
   revision ID, tidak menyimpan API key, dan mengunci `story:<story_id>`.
9. Critic `REVISE` atau continuity `BLOCKED` masuk `REVIEW_REQUIRED`; operator
   harus membuat brief/revisi baru, bukan memaksa retry artifact yang sama.

Lock hanya berlaku jika pekerjaan masuk melalui Supervisor. Menjalankan
`podcast_clipper.py render` langsung tetap didukung, tetapi dapat melewati queue
dan tidak boleh dijalankan bersamaan pada job yang sama.

## Operasi Windows

Dari `D:\Hermes\video-agent`:

```powershell
# Antrekan seluruh clip pada satu plan. Simpan task_id dari output JSON.
& ".\.venv\Scripts\python.exe" scripts\supervisor.py enqueue clip-video `
  --plan "jobs\JOB_ID\clip-plan.json"

# Atau hanya clip tertentu.
& ".\.venv\Scripts\python.exe" scripts\supervisor.py enqueue clip-video `
  --plan "jobs\JOB_ID\clip-plan.json" --clip clip-01 --clip clip-03

# Antrekan story development melalui endpoint model lokal.
& ".\.venv\Scripts\python.exe" scripts\supervisor.py enqueue story `
  --brief "stories\episode-001\brief.json" `
  --model $env:HERMES_LLM_MODEL

# Proses satu task lalu kembali ke prompt.
& ".\.venv\Scripts\python.exe" scripts\supervisor.py worker --once

# Worker terus hidup sampai Ctrl+C.
& ".\.venv\Scripts\python.exe" scripts\supervisor.py worker

# Baca snapshot dan history task.
& ".\.venv\Scripts\python.exe" scripts\supervisor.py list --job JOB_ID
& ".\.venv\Scripts\python.exe" scripts\supervisor.py show TASK_ID
& ".\.venv\Scripts\python.exe" scripts\supervisor.py events --task TASK_ID

# Korelasikan run clipper dengan task Supervisor.
& ".\.venv\Scripts\python.exe" scripts\podcast_clipper.py runs --task TASK_ID
```

Global option harus ditulis sebelum subcommand bila database custom dipakai:

```powershell
& ".\.venv\Scripts\python.exe" scripts\supervisor.py `
  --db "D:\Hermes\state\supervisor.db" list
```

## Recovery sesudah worker mati

Worker baru otomatis merekonsiliasi lease kedaluwarsa saat mencoba claim. Untuk
melakukannya tanpa menjalankan task:

```powershell
& ".\.venv\Scripts\python.exe" scripts\supervisor.py recover
& ".\.venv\Scripts\python.exe" scripts\supervisor.py list `
  --state RECOVERY_REQUIRED
```

Periksa bahwa tidak ada proses FFmpeg/Python lama dan tinjau artifact/manifest.
Jika aman dan attempt budget masih tersedia:

```powershell
& ".\.venv\Scripts\python.exe" scripts\supervisor.py retry TASK_ID
& ".\.venv\Scripts\python.exe" scripts\supervisor.py worker --once
```

Task yang sudah menghabiskan `max_attempts` tidak dapat di-retry melalui record
yang sama. Setelah penyebab diperbaiki, enqueue pekerjaan baru dengan
`--idempotency-key` baru agar keputusan operator eksplisit dan dapat diaudit.

## Batas checkpoint

Supervisor ini belum merupakan Windows Service, belum auto-start saat boot,
dan belum memiliki pause/cancel process tree. Queue menyediakan adapter
`CLIP_VIDEO` dan `DEVELOP_STORY`; image/video generation, voice, audio, assembly,
QA, dan publisher akan menjadi adapter terpisah pada checkpoint berikutnya.

Recovery lease tidak menebak apakah PID lama masih hidup. Karena itu task crash
diblokir untuk inspeksi, bukan auto-resume. Dashboard belum tersedia; output JSON
`list`, `show`, `events`, dan event clipper menjadi kontrak backend awal untuk UI.
