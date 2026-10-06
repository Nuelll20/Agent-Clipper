# Checkpoint Hermes — 6 Oktober 2026

## Acuan

Repository: https://github.com/Nuelll20/Agent-Clipper.
Baseline: `eb5596b` di main, hasil merge PR #2 render/delivery separation.
Branch pekerjaan: `feat/artifact-contract-gate`.

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

## Verifikasi

Verifikasi branch artifact pada Linux: **43 passed, 1 skipped**. Skip merupakan
pemeriksaan font produksi Windows. Tes targeted kontrak dan area terdampak:
**22 passed**. Compile check dan diff whitespace check berhasil.

```powershell
& ".\.venv\Scripts\python.exe" -m pytest -q
& ".\.venv\Scripts\python.exe" scripts\telegram_approval.py self-test
```

Pengujian cloud dilakukan di Linux, bukan laptop pengguna. Tes batch menggunakan
mock rendering dan upload. Belum ada verifikasi video penuh, Whisper, NVENC,
Remotion, font Windows, atau Telegram live.

## Langkah berikutnya

Sesudah perubahan ini diverifikasi dan di-merge, Checkpoint C selesai. Berikutnya
bangun Supervisor minimal dengan locking job, heartbeat/recovery, resource queue,
dan retry terbatas sebelum dashboard mengklaim status live.

Snapshot STARTING/WORKING atau manifest delivery `sending` yang ditinggalkan proses
mati belum direkonsiliasi otomatis. Database event hanya observabilitas; bukan
auto-resume. COMPLETED tidak berarti approved atau published. Jangan memulai dua
writer pada job sama. Fingerprint belum menjadi kebijakan invalidasi otomatis;
detail batas kontrak tercatat dalam `ARTIFACT_CONTRACT.md`.

Git push memerlukan persetujuan sesuai permission model handoff pengguna.
Jangan mengirim Telegram atau mempublikasikan video sebagai bagian smoke test.
