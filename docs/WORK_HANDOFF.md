# Checkpoint Hermes — 6 Oktober 2026

## Acuan

Repository: https://github.com/Nuelll20/Agent-Clipper.
Baseline: `68d75ec` di main, hasil merge PR #6 asset pipeline.
Branch pekerjaan: `feat/comfyui-reference-provider`.

Source of truth tujuan: handoff pengguna, evolusi clipper existing menuju
AI Workforce + Animation Studio + AI Office. Clipper harus tetap berfungsi.
Perubahan checkpoint ini belum di-push ke GitHub.

## Selesai

- Checkpoint B event/state sudah di-merge melalui PR #1.
- Pemisahan render/delivery sudah di-merge melalui PR #2.
- Render selesai dicatat sebelum Telegram dijalankan.
- Kegagalan Telegram tidak lagi mengubah klip render menjadi failed.
- CLI `deliver` mengirim ulang artifact dari manifest tanpa menjalankan renderer.
- Kunci delivery unik mencegah upload ulang biasa di database approval.
- Hasil upload yang ambigu diblokir dari retry otomatis; `--force` bersifat eksplisit.
- Database approval existing dimigrasikan otomatis.
- Dependensi Python langsung tercatat dalam requirements produksi dan development.
- Panduan operasional: `DELIVERY_WORKFLOW.md`.
- Validator artifact sekarang fail-closed dan mengembalikan exit code gagal.
- Transcript legacy diadaptasi ke envelope canonical `2.0` tanpa membuang field.
- Gate transcript aktif pada ingest, awal render, dan transcript per klip.
- `job.json` memakai kontrak dan schema v2 tervalidasi.
- Panduan kontrak: `ARTIFACT_CONTRACT.md`.
- Checkpoint C sudah di-merge melalui PR #3 dan lulus 44 tes di Windows.
- Queue task Supervisor tersimpan di SQLite dan enqueue bersifat idempotent.
- Lease job/resource mencegah dua worker Supervisor menulis job yang sama.
- Heartbeat memagari ownership; lease mati menjadi `RECOVERY_REQUIRED`.
- Retry proses memakai backoff dan berhenti pada `max_attempts`.
- Adapter `CLIP_VIDEO` memakai renderer existing dengan `--no-send`.
- Checkpoint D sudah di-merge melalui PR #4 dan lulus 55 tes di Windows.
- Story brief dan enam stage story memakai artifact canonical `2.0`.
- Worldbuilder, plotter, screenwriter, critic, continuity, dan shot planner
  menghasilkan lineage serta event agent yang dapat diaudit.
- Critique `REVISE` dan continuity `BLOCKED` berhenti di `REVIEW_REQUIRED`.
- Approval manusia terikat fingerprint manifest revisi dan fail-closed jika
  artifact berubah sesudah review.
- Adapter Supervisor `DEVELOP_STORY` memakai brief fingerprint dan tidak
  menyimpan API key provider.
- Checkpoint E sudah di-merge melalui PR #5 dan lulus 72 tes di Windows.
- Voice cast mengunci identitas suara seluruh karakter pada story revision.
- Asset plan memecah reference, animation, optional voice, dan SFX per shot.
- Attempt immutable menyimpan fingerprint request/output/dependency.
- Regenerasi selektif memakai ID stabil sehingga retry tidak menggandakan take.
- Reference baru membuat animation lama stale tanpa mengulang shot lain.
- Provider command lokal tidak memakai shell dan secret tetap di environment.
- Adapter Supervisor `GENERATE_ASSETS` menjalankan generation tanpa delivery.
- ComfyUI 0.39.0, SDXL base, CLIP Vision, dan IP-Adapter telah diverifikasi pada
  RTX 4060 Laptop 8 GB melalui API lokal `127.0.0.1:8188` dalam mode low-VRAM.
- Bridge command reference ComfyUI membangun workflow SDXL atau IP-Adapter,
  memverifikasi reference hash, dan mengambil PNG hasil ke attempt Checkpoint F.
- Health check bridge memvalidasi checkpoint serta model/node IP-Adapter langsung
  melalui metadata ComfyUI sebelum production dijalankan.

## File berubah

| File | Alasan |
|---|---|
| `scripts/podcast_clipper.py` | Status delivery, manifest-first render, dan CLI retry `deliver` |
| `scripts/telegram_approval.py` | Migrasi delivery key dan perlindungan pengiriman duplikat/ambigu |
| `tests/test_delivery_separation.py` | Failure, retry, idempotensi, force, dan migrasi database |
| `tests/test_clipper_observation.py` | Penyesuaian kontrak render terpisah |
| `requirements.txt`, `requirements-dev.txt` | Dependensi langsung yang telah diuji |
| `docs/DELIVERY_WORKFLOW.md` | Operasi dan batas keselamatan retry |
| `docs/EVENT_SYSTEM.md` | Semantik event sesudah delivery dipisahkan |
| `docs/WORK_HANDOFF.md` | Status dan kelanjutan pekerjaan |
| `core/artifacts.py`, `validators/artifact_validator.py` | Adapter v2 dan validator fail-closed |
| `artifacts/schemas/*.schema.json` | Kontrak canonical transcript, job manifest, dan scene graph |
| `scripts/transcribe_pro.py` | Output transcriber melewati gate yang sama |
| `tests/test_artifact_contract.py` | Kompatibilitas legacy dan failure-path produksi |
| `docs/ARTIFACT_CONTRACT.md` | Kontrak, titik gate, operasi, dan batas checkpoint |
| `core/supervisor.py` | Queue, task state, resource lease, heartbeat, recovery, dan worker |
| `scripts/supervisor.py` | CLI enqueue/worker/list/show/events/retry/recover |
| `tests/test_supervisor.py` | Idempotensi, fencing, recovery, retry, adapter, dan CLI |
| `docs/SUPERVISOR.md` | Operasi dan batas keselamatan Supervisor |
| `core/story.py` | Kontrak semantic, provider, pipeline, lineage, dan approval story |
| `scripts/story_studio.py` | CLI brief/develop/validate/review/approval-check |
| `artifacts/schemas/story-*.schema.json` | Kontrak brief, stage, manifest, dan review |
| `tests/test_story_pipeline.py` | Gate, resume, tamper, approval, event, dan adapter Supervisor |
| `docs/STORY_PIPELINE.md` | Operasi dan batas Checkpoint E |
| `core/production.py` | Voice cast, plan, provider, attempts, manifest, selective regeneration |
| `scripts/animation_studio.py` | CLI cast/prepare/generate/validate |
| `artifacts/schemas/asset-*.schema.json` | Kontrak provider, plan, attempt, dan manifest |
| `artifacts/schemas/voice-cast.schema.json` | Kontrak identitas suara karakter |
| `tests/test_asset_pipeline.py` | Approval, lineage, failure, provider, dan regenerasi |
| `docs/ASSET_PIPELINE.md` | Operasi dan batas Checkpoint F |
| `scripts/providers/comfyui_reference.py` | Provider reference SDXL/IP-Adapter melalui API lokal ComfyUI |
| `config/asset-providers.comfyui.example.json` | Template command provider ComfyUI tanpa secret |
| `tests/test_comfyui_reference_provider.py` | Workflow, validasi, hash, dan output atomic bridge |

## Verifikasi

Verifikasi baseline asset pada Linux: **87 passed, 1 skipped**. Skip merupakan tes
font produksi Windows. Bridge ComfyUI menambah enam tes stdlib untuk workflow SDXL,
workflow IP-Adapter, validasi input/hash, output atomic, upload, polling history,
dan download `/view`. Integrasi offline Supervisor menjalankan generation awal dan
regenerasi selektif sebagai subprocess.

```powershell
& ".\.venv\Scripts\python.exe" -m pytest -q
& ".\.venv\Scripts\python.exe" scripts\telegram_approval.py self-test
```

Pengujian cloud dilakukan di Linux, bukan laptop pengguna. Tes batch menggunakan
mock rendering dan upload. Belum ada verifikasi video penuh, Whisper, NVENC,
Remotion, font Windows, atau Telegram live.

## Langkah berikutnya

Verifikasi bridge ComfyUI di laptop dan generate satu reference melalui asset plan.
Sesudah provider video, TTS, dan SFX nyata dipilih, Checkpoint G membangun assembly
timeline dan technical QA atas media nyata.

Task dengan heartbeat mati direkonsiliasi ke `RECOVERY_REQUIRED`, tetapi tidak
auto-resume. Pastikan proses lama berhenti sebelum retry manual. Lock hanya berlaku
untuk pekerjaan melalui Supervisor; jangan menjalankan CLI langsung bersamaan pada
job yang sama. COMPLETED tidak berarti approved atau published. Story stage sudah
memeriksa lineage fingerprint; kebijakan invalidasi clipper lama belum otomatis.

Git push memerlukan persetujuan sesuai permission model handoff pengguna.
Jangan mengirim Telegram atau mempublikasikan video sebagai bagian smoke test.
