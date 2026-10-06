# Artifact contract dan validation gate — checkpoint C, diperluas checkpoint E

Checkpoint ini menetapkan envelope artifact canonical `2.0` tanpa memutus
transcript lama yang sudah dibuat Agent-Clipper. Gate yang aktif saat ini
mencakup transcript dan `job.json`. Schema scene graph sudah memakai envelope
yang sama, tetapi scene graph belum menjadi output pipeline produksi.

## Envelope canonical

Artifact v2 memiliki field berikut:

| Field | Makna |
|---|---|
| `artifact_type` | Jenis artifact, misalnya `transcript` atau `job-manifest` |
| `schema_version` | Versi kontrak; canonical saat ini adalah string `2.0` |
| `producer_version` | Versi producer Agent-Clipper |
| `source_fingerprint` | SHA-256 sumber media |
| `config_fingerprint` | SHA-256 konfigurasi producer yang diketahui |
| `created_at` | Timestamp UTC berformat ISO 8601 |
| `upstream_artifacts` | Fingerprint artifact input langsung |

Schema tetap mengizinkan field tambahan agar consumer existing tidak kehilangan
metadata Whisper, hook, headline, atau field editor lain.

## Adapter transcript lama

`core/artifacts.py` menerima transcript tanpa versi serta versi legacy `1`,
`2`, `"1"`, `"2"`, dan `"1.0"`. Adapter mempertahankan segmen existing lalu
menambahkan envelope, metadata turunan, fingerprint, dan penanda
`contract_adapter`. Versi yang tidak dikenal ditolak; artifact yang sudah
mengaku canonical `2.0` tidak diperbaiki diam-diam bila field wajibnya rusak.

Saat `ingest` memakai kembali transcript lama, atau `render` membaca transcript
lama, hasil adaptasi yang lolos gate ditulis kembali secara atomik sebagai v2.
Output baru `transcribe_pro.py` juga melewati adapter dan gate sebelum ditulis.

## Gate produksi

Gate berjalan pada batas berikut:

1. sesudah transkripsi/reuse pada `ingest`, sebelum `job.json` dan clip plan;
2. pada `job.json` sebelum manifest tersebut ditulis;
3. sebelum batch `render`, sehingga transcript rusak tidak memulai renderer;
4. sesudah transcript dipotong per klip, sebelum generator subtitle.

Kegagalan schema atau semantic check menjadi `WorkflowError`, mengembalikan exit
code gagal dari CLI, dan membuat run event berakhir `FAILED`. Validator tidak lagi
sekadar mencetak `[BLOCK]` lalu melanjutkan pipeline.

Selain JSON Schema, transcript diperiksa untuk segmen kosong, angka non-finite,
rentang waktu terbalik, urutan waktu, durasi yang tidak mencakup segmen, teks
kosong, raw ASS override tag, karakter NUL/escape, serta timestamp kata invalid.

## Pemeriksaan manual

```powershell
& ".\.venv\Scripts\python.exe" validators\artifact_validator.py `
  jobs\JOB_ID\transcript-full.json `
  artifacts\schemas\transcript.schema.json
```

Exit code `0` berarti schema lolos. Exit code `1` berarti artifact diblokir.
Untuk pemeriksaan semantic lengkap, jalankan jalur `ingest` atau `render`.

## Batas checkpoint

Fingerprint canonical divalidasi bentuknya, tetapi belum dibandingkan kembali
dengan isi source/config pada setiap reuse. Karena itu perubahan source atau
opsi transcriber belum otomatis menginvalidasi artifact canonical existing.
Supervisor checkpoint D menyediakan lock satu writer per job/resource dan
recovery lease untuk pekerjaan yang masuk queue. Kebijakan invalidasi fingerprint
dan attempt-isolated staging artifact masih belum diterapkan.

Gate ini belum mencakup semua artifact editor, render manifest, EDL, QA report,
atau scene graph produksi. Jangan menyimpulkan seluruh pipeline memiliki kontrak
v2 hanya karena transcript dan job manifest sudah memilikinya.

## Artifact story

Checkpoint E menambahkan schema untuk `story-brief`, enam `story-<stage>`,
`story-manifest`, dan `story-review`. Selain JSON Schema, semantic gate memeriksa
referensi ID lintas universe/outline/screenplay/shot list, verdict critic,
continuity, urutan stage, dan SHA-256 setiap upstream artifact.

Approval terikat pada fingerprint manifest. Mengubah manifest atau stage setelah
approval memblokir gate produksi. Detail operasi dan batasnya ada di
`STORY_PIPELINE.md`.

Checkpoint F menambahkan `voice-cast`, `asset-plan`, `asset-attempt`, dan
`asset-manifest`. Attempt bersifat immutable dan menyimpan fingerprint request,
output, dependency, provider, serta regeneration key. Animation ditautkan ke
reference shot tertentu sehingga reference baru tidak dapat memakai animation
lama secara diam-diam. Detail ada di `ASSET_PIPELINE.md`.
