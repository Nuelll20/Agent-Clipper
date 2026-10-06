# Audit Agent-Clipper → Hermes Studio

Tanggal: 5 Oktober 2026. Sumber: https://github.com/Nuelll20/Agent-Clipper,
branch `main`, commit `8bb9bf2dc21d9a6ea31a3c02e46270ec65f0dfd4`.
Audit ini menggantikan audit awal ZIP 28 September. Perubahan yang hanya ada
di laptop pengguna tidak termasuk cakupan. Handoff Clipper → Animation Studio
dalam percakapan menjadi arah pengembangan.

Pembaruan 6 Oktober 2026: event/state sudah di-merge melalui PR #1 dan pemisahan
render/delivery melalui PR #2. Branch `feat/artifact-contract-gate` menyelesaikan
dua temuan kontrak artifact P1 dengan adapter legacy dan gate fail-closed; detail
implementasi ada di `ARTIFACT_CONTRACT.md`.

## 1. Struktur dan entry point

| Lokasi | Peran aktual |
|---|---|
| `scripts/podcast_clipper.py` | CLI dan orchestrator: ingest, validate, render, watch, status, listener, campaign, doctor |
| `scripts/transcribe_pro.py` | faster-whisper, word timestamps, JSON dan teks |
| `scripts/make_cinematic_ass_pro.py` | Caption ASS, analisis frame, edit plan, validasi ASS dan font |
| `scripts/editorial_pro.py`, `visual_explainer_pro.py` | Rencana editorial, spotlight, visual pendukung |
| `scripts/render_cinematic_pro.py`, `render_editorial_pro.py` | Rendering FFmpeg, audio/SFX, fallback encoder |
| `scripts/motioncraft_bridge.py` | Rencana motion dan pemanggilan Remotion |
| `motioncraft-renderer/src/hermes/` | Composition HermesSemanticMotion |
| `scripts/campaign_pro.py` | Aset, aturan brand, preflight, compliance |
| `scripts/telegram_approval.py` | Kirim review, callback pemilik, status SQLite |
| `validators/`, `artifacts/schemas/`, `fonts/` | Fondasi validasi dan kontrak artifact |
| `tests/` | Caption safety, ASS, font, penempatan overlay |
| `skills/podcast-video-clipper/SKILL.md` | Panduan operasi clipper untuk Hermes |

Tidak ditemukan AGENTS.md atau manifest dependensi Python di snapshot ini.
Dependensi renderer tercatat dalam package.json dan package-lock.json.

## 2. Pipeline aktual

1. `command_ingest()` mengunduh melalui yt-dlp, memeriksa media dengan ffprobe,
   menjalankan transkripsi, lalu menulis job.json dan template clip-plan.json.
2. Template awal berisi `clips: []`. Pemilihan highlight memerlukan perencanaan
   oleh operator/agent; CLI belum otomatis menghasilkan daftar highlight lengkap.
3. `command_render()` memvalidasi rencana dan menjalankan klip secara berurutan.
4. `render_one_clip()` melakukan crop/framing, transcript slicing, subtitle,
   editorial, visual, optional MotionCraft, rendering, dan optional campaign.
5. Hasil dikirim ke Telegram secara default kecuali `--no-send`.
6. render-manifest.json menyimpan hasil per klip. Approval memiliki database
   SQLite terpisah. Perintah `status` saat ini berarti status approval.

## 3. Komponen yang dipertahankan dan reused

Pertahankan CLI existing, struktur job, subtitle/font gates, encoder fallback,
motion layout fixes, campaign compliance, dan approval dengan pemeriksaan
owner/chat. Reuse renderer melalui adapter, bukan memindahkan seluruh script.
`--clip` sudah memungkinkan render ulang klip terpilih; ini dasar yang baik
untuk selective regeneration, tetapi belum checkpoint per tahap.

Transkripsi saat ini memilih CPU int8 secara eksplisit dalam `load_model()`.
Jangan mengasumsikan GPU Whisper aktif berdasarkan environment lama.
MotionCraft adalah renderer motion graphics; belum merupakan provider AI
image-to-video atau animator cerita.

## 4. Coupling dan masalah prioritas

| Prioritas | Bukti di kode | Dampak dan tindak lanjut |
|---|---|---|
| P1 | `render_one_clip()` mencakup produksi dan Telegram; `command_render()` menangkap keduanya sebagai kegagalan klip | Video yang sudah selesai dapat tercatat gagal ketika pengiriman gagal. Pisahkan status render dan delivery sebelum retry otomatis. |
| P1 | `transcribe_pro.py` menghasilkan schema_version integer 2; transcript.schema.json meminta string serta metadata yang tidak dihasilkan | Kontrak artifact belum kompatibel. Tambah adapter/envelope versioned sebelum enforcement; jangan langsung memblokir semua output lama. |
| P1 | `validate_artifact()` menangkap ValidationError lalu hanya mencetak BLOCK | Pemanggil tidak memperoleh exception atau hasil terstruktur untuk menghentikan pipeline. Perlu gate eksplisit dan tes integrasi. |
| P1 | Tidak ada event store, lifecycle task umum, worker lease atau scheduler | UI belum dapat memulihkan state produksi secara andal. Tambah event/state lebih dahulu. |
| P2 | `write_json()` memakai nama temporary tetap; manifest read-modify-write tanpa lock job | Proses paralel pada job sama dapat saling menimpa. Pertahankan satu worker per job sampai locking tersedia. |
| P2 | Reuse source/transcript berdasarkan keberadaan file | Perubahan sumber/model/config dapat memakai artifact lama. Tambahkan fingerprint dan kebijakan invalidasi. |
| P2 | `stage_props()` menamai staging berdasarkan path, bukan attempt | Render konkuren untuk path sama dapat berbagi aset. Isolasi per run/attempt sebelum parallel rendering. |
| P2 | `resolve_plan_file()` mengizinkan path absolut/relatif di luar job | Wajar untuk CLI lokal tepercaya, tetapi backend agent nanti perlu allowlist workspace. |
| P2 | run_command memakai subprocess sinkron, tanpa timeout umum atau cancellation protocol | Pause/cancel tidak boleh sekadar mengubah UI; perlu kendali proses nyata. |
| P2 | Tidak ada requirements/pyproject dan panduan bootstrap di root | Instalasi baru belum reproducible; rekam dependency yang benar-benar teruji di Windows. |

Validator transcript/subtitle mandiri tersedia, tetapi pencarian referensi tidak
menemukan pemanggilannya dalam jalur produksi scripts. Gate ASS dan font memang
dipanggil oleh generator subtitle. Jangan menyamakan keduanya dengan QA visual
menyeluruh. Pemilihan zona motion memakai waktu anchor; belum membuktikan bebas
benturan sepanjang durasi overlay.

## 5. Kesenjangan terhadap handoff

Sudah ada produksi clip, motion, artifact file, validasi dasar, dan approval.
Belum ditemukan Supervisor umum, task dependency scheduler, story/continuity
pipeline, AnimationProvider, universe memory, AI Office API/UI, publisher
YouTube, atau analytics pipeline pada snapshot ini.

Integrasi Hermes utama di luar repository ini belum diaudit. Jangan membangun
pengganti Hermes core tanpa memeriksa kontrak integrasinya terlebih dahulu.

## 6. Integrasi event dan Supervisor

Tambahkan paket `core/` di repository existing. Event persisten dicatat pada
batas kerja nyata: ingest, unduh, transkripsi, per klip, dan hasil akhir.
Gunakan run_id terpisah dari job_id sehingga rerun tidak menimpa sejarah.
State dan event harus ditulis dalam satu transaksi SQLite.

Supervisor nantinya memanggil adapter Clipper; CLI lama tetap tersedia. Provider
animation menjadi adapter lain. UI membaca snapshot dan urutan event dari
backend; eksekusi produksi tidak bergantung pada browser. WebSocket menjadi
transport, bukan penyimpanan utama. Progress memakai unit selesai/total;
jika total belum diketahui, tampilkan stage tanpa persentase.

## 7. Baseline verifikasi

Lingkungan audit: Linux, Python 3.12; berbeda dari produksi Windows Python 3.11.
Dependensi audit yang dipasang: pytest 9.1.1, OpenCV headless 5.0.0.93,
jsonschema 4.26.0; NumPy runtime 2.3.5. Ini bukan lockfile produksi yang disarankan.

- `python3 -m pytest -q`: **13 passed, 1 skipped**. Skip merupakan tes font Windows.
- `python3 scripts/telegram_approval.py self-test`: **OK**, tanpa akses Telegram.
- CLI `--help`: berhasil.
- AST parse 25 file Python: berhasil.
- Probe bentuk transcript terhadap schema: gagal pada metadata wajib dan tipe
  schema_version, sesuai temuan P1. Probe memakai representasi field output,
  bukan hasil transkripsi media baru.
- Probe artifact tidak valid: validator mencetak BLOCK tetapi kembali dengan None.

Belum diverifikasi: download/transkripsi nyata, rendering Remotion/FFmpeg penuh,
GPU/NVENC, font produksi Windows, dan callback Telegram live. Tidak ada pesan
Telegram atau publikasi YouTube yang dilakukan selama audit.

## 8. Roadmap migrasi dengan checkpoint

| Checkpoint | Hasil | Gate penyelesaian |
|---|---|---|
| A — audit | Peta current architecture dan baseline | Temuan di atas tercatat; kode produksi belum diubah saat baseline |
| B — event/state foundation | SQLite event log + snapshot, run identity, observasi clipper | Persistensi setelah reopen, isolasi run, rollback transaksi, tes existing tetap lulus |
| C — stabilisasi produksi | Status render terpisah dari delivery, artifact contract adapter, dependensi | Pengiriman gagal tidak memicu render ulang; artifact salah benar-benar diblokir |
| D — Supervisor minimal | CLIP_VIDEO adapter, queue, retry terbatas, resource lease | Retry tidak menduplikasi side effect; restart merekonsiliasi worker mati |
| E — story | Universe, outline, screenplay, kritik, continuity, shot list | Artifact terstruktur dan review script sebelum produksi |
| F — assets/animation/audio | Reference per shot, provider abstraction, voice identity, SFX | Satu shot gagal dapat diregenerasi tanpa mengulang episode |
| G — assembly dan QA | Reuse editor, timeline, technical QA | Durasi, stream, corrupt/black frames dan safe area diperiksa |
| H — Office | State API, WebSocket replay, cards; kemudian animasi 2D | Browser reconnect menampilkan state nyata; worker berjalan tanpa UI |
| I — controls/publishing | Pause/retry/approve lewat backend, YouTube approval gate | Otorisasi, versi artifact yang disetujui, dan idempotency sebelum publish |
| J — analytics | Metrik produksi/publikasi ke Showrunner | Insight berbasis data yang tersedia, tanpa skor rekaan |

Checkpoint B mulai dari observabilitas. Ia belum menjanjikan auto-resume,
cancellation, task scheduling, atau API/WebSocket. Implementasi bertahap ini
mempertahankan urutan event-first dalam handoff.

Status 6 Oktober 2026: implementasi Checkpoint C lulus **43 tes**, dengan satu
tes font Windows dilewati di Linux. Verifikasi Windows tetap diperlukan sebelum
merge. Checkpoint berikutnya adalah D — Supervisor minimal.
