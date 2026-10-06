# Multi-agent story pipeline — checkpoint E

Checkpoint E menambahkan tahap pengembangan cerita sebelum assets dan animasi.
Ia tidak mengganti clipper. Enam role menghasilkan artifact secara berurutan:

| Urutan | Agent | Artifact |
|---|---|---|
| 1 | `worldbuilder` | universe, aturan dunia, karakter, dan lokasi |
| 2 | `plotter` | logline, tema, act, dan beat |
| 3 | `screenwriter` | scene, action, dialogue, dan estimasi durasi |
| 4 | `story-critic` | verdict serta issue terstruktur |
| 5 | `continuity-editor` | pemeriksaan kontinuitas lintas scene |
| 6 | `shot-planner` | shot list yang merujuk scene/karakter/lokasi canonical |

Setiap output memakai envelope artifact `2.0`, menyimpan fingerprint brief,
konfigurasi provider, seluruh upstream artifact, identitas revisi, dan agent.
Artifact ditulis atomik. Retry task yang sama memakai ulang stage tervalidasi,
bukan memanggil model lagi tanpa alasan.

## Gate

- ID karakter, lokasi, scene, dan urutan shot diperiksa lintas artifact.
- `critique.verdict=REVISE` menghentikan pipeline sebelum continuity.
- `continuity.status=BLOCKED` menghentikan pipeline sebelum shot list.
- Manifest hanya dibuat jika keenam tahap lengkap dan valid.
- Approval merujuk SHA-256 manifest tertentu. Perubahan manifest atau stage
  setelah review membuat gate produksi gagal.
- `CHANGES_REQUESTED` tidak dapat diperlakukan sebagai approval.
- Revisi yang berubah harus memakai `revision_id` dan enqueue baru.

Adapter animasi checkpoint F wajib memanggil `assert_story_approved()` atau CLI
`approval-check` sebelum membuat asset. Checkpoint ini belum memanggil generator
gambar/video, voice, renderer, Telegram, atau YouTube.

## Provider

Provider produksi awal adalah endpoint Chat Completions OpenAI-compatible. Default
base URL sesuai proxy lokal Hermes: `http://127.0.0.1:20128/v1`. API key dibaca
dari environment worker dan tidak disimpan dalam queue, artifact, event, atau
fingerprint konfigurasi.

Provider `fixture` hanya untuk test dan smoke offline. Kontennya deterministik dan
bukan hasil kreatif untuk produksi.

```powershell
$env:HERMES_LLM_BASE_URL = "http://127.0.0.1:20128/v1"
$env:HERMES_LLM_MODEL = "NAMA_MODEL_YANG_TERSEDIA"
$env:HERMES_LLM_API_KEY = "API_KEY_JIKA_ENDPOINT_MEMERLUKAN"
```

## Alur Windows

Buat brief:

```powershell
& ".\.venv\Scripts\python.exe" scripts\story_studio.py init `
  --story episode-001 `
  --title "Judul Episode" `
  --premise "Premise cerita yang ingin dikembangkan." `
  --audience "remaja" `
  --format "short-animation" `
  --duration 60 `
  --constraint "Tanpa kekerasan grafis"
```

Antrekan melalui Supervisor dan simpan `task_id` dari output:

```powershell
& ".\.venv\Scripts\python.exe" scripts\supervisor.py enqueue story `
  --brief "stories\episode-001\brief.json" `
  --model $env:HERMES_LLM_MODEL

& ".\.venv\Scripts\python.exe" scripts\supervisor.py worker --once
```

Revision ID dari task Supervisor sama dengan `task_id`. Validasi dan tinjau file
`universe.json`, `outline.json`, `screenplay.json`, `critique.json`,
`continuity.json`, dan `shot-list.json`, lalu jalankan:

```powershell
$manifest = "stories\episode-001\revisions\TASK_ID\story-manifest.json"

& ".\.venv\Scripts\python.exe" scripts\story_studio.py validate `
  --manifest $manifest

& ".\.venv\Scripts\python.exe" scripts\story_studio.py review `
  --manifest $manifest `
  --decision approve `
  --reviewer "Nuel"

& ".\.venv\Scripts\python.exe" scripts\story_studio.py approval-check `
  --manifest $manifest
```

Untuk menolak revisi, gunakan `--decision request-changes --note "alasan"`.
Jangan mengedit stage dalam revisi lama lalu mempertahankan approval. Perbaiki
brief atau prompt/provider, kemudian enqueue revisi baru agar lineage dapat diaudit.

## Status dan failure

Kegagalan jaringan, respons JSON invalid, atau proses provider nonzero dapat
memakai bounded retry Supervisor. Critic `REVISE` dan continuity `BLOCKED`
berakhir sebagai `REVIEW_REQUIRED`, bukan retry otomatis. Artifact parsial tetap
ada untuk pemeriksaan. Lease kedaluwarsa tetap mengikuti prosedur
`RECOVERY_REQUIRED` dalam `SUPERVISOR.md`.

Event story memakai `unit=stages`; tiap event menyimpan agent sebenarnya. Run
review manusia dicatat terpisah sebagai `STORY_REVIEW`. `COMPLETED` pada task
development berarti package cerita selesai dibuat, bukan sudah disetujui dan
bukan sudah diproduksi menjadi animasi.

## Batas checkpoint

Belum ada loop revisi otomatis, memory universe lintas episode, budget token,
streaming response, provider Responses API, asset generation, voice identity,
animasi per shot, assembly, atau dashboard. Prompt kreatif awal sengaja ringkas;
kualitas cerita perlu diuji dengan model lokal yang dipilih pengguna sebelum
style bible dan prompt library distabilkan.
