# Shot assets, animation, voice, dan SFX — checkpoint F

Checkpoint F mengubah shot list yang telah disetujui menjadi unit produksi
independen. Setiap shot memiliki `reference`, `animation`, optional `voice`, dan
`sfx`. Output tidak digabung menjadi episode pada tahap ini; assembly dan
technical QA berada di Checkpoint G.

## Pagar produksi

1. Story revision harus memiliki review `APPROVED` yang masih cocok dengan
   fingerprint manifest.
2. Voice cast harus mencakup tepat semua karakter. Voice ID, provider, bahasa,
   style, dan optional reference audio dikunci sebelum plan dibuat.
3. Asset plan direkonstruksi saat dibaca dan harus identik dengan story, cast,
   video profile, serta provider config.
4. Setiap generation menulis directory attempt baru. Request, output, dan
   attempt JSON memiliki fingerprint; file lama tidak ditimpa.
5. Animation bergantung pada fingerprint reference shot. Reference baru membuat
   animation lama `STALE` sampai diregenerasi.
6. `regeneration_id` menjadi bagian input fingerprint. Retry task yang sama
   reuse attempt; ID baru menghasilkan take baru secara eksplisit.
7. Satu asset gagal tidak menghapus hasil shot/asset lain. Dengan
   `--continue-on-error`, semua unit independen tetap dicoba dan manifest akhir
   menyebut kegagalan secara nyata.

`COMPLETED` berarti seluruh asset plan memiliki file non-kosong dan lineage yang
valid. Validasi codec, stream, black frame, loudness, serta kualitas visual belum
termasuk; itu gate Checkpoint G.

## Provider lokal

Mode `command` menjalankan executable lokal tanpa shell. Untuk setiap kind,
provider menerima path request dan output melalui placeholder `{request}` serta
`{output}`. Jika placeholder tidak ditulis, CLI otomatis menambahkan
`--request PATH --output PATH`. Environment berikut juga tersedia:

- `HERMES_ASSET_KIND`
- `HERMES_ASSET_REQUEST`
- `HERMES_ASSET_OUTPUT`

Salin `config/asset-providers.example.json`, lalu ganti executable dengan bridge
ComfyUI/diffusion, image-to-video, TTS, dan SFX yang benar-benar terpasang di
laptop. Jangan menaruh API key, token, atau password di file config; provider
membaca credential dari environment-nya sendiri.

Mode `fixture` hanya menulis byte deterministik untuk test orchestration. File
berekstensi PNG/MP4/WAV dari fixture **bukan media valid** dan tidak boleh dipakai
sebagai hasil produksi.

### Bridge reference ComfyUI

Repository menyediakan bridge nyata untuk asset `reference` di
`scripts/providers/comfyui_reference.py`. Bridge membaca request Checkpoint F,
mengirim workflow SDXL ke ComfyUI lokal, menunggu history selesai, mengunduh PNG
melalui endpoint `/view`, lalu menulis output attempt secara atomik. Server selain
loopback ditolak agar ComfyUI tidak terbuka ke jaringan tanpa sengaja.

Health check SDXL:

```powershell
& ".\.venv\Scripts\python.exe" scripts\providers\comfyui_reference.py `
  --server "http://127.0.0.1:8188" `
  --checkpoint "sd_xl_base_1.0.safetensors" `
  --require-ipadapter `
  --health-check
```

Salin `config/asset-providers.comfyui.example.json` menjadi
`config/asset-providers.local.json`, lalu perbaiki kedua path pada command
`reference` dan `animation`. Provider TTS dan SFX tetap berupa placeholder sampai
provider nyata dipasang. Karena itu, gunakan generation selektif `--asset
reference --asset animation`; jangan menjalankan semua kind sebelum kedua
provider audio siap.

IP-Adapter bersifat opsional. Tambahkan argumen berikut pada command `reference`:

```json
"--reference-image", "E:\\path\\to\\immutable-reference.png",
"--reference-sha256", "HEX_SHA256_TANPA_PREFIX"
```

Bridge mengunggah file ke input ComfyUI dengan nama berbasis hash, memverifikasi
hash sebelum setiap generation, lalu memakai `IPAdapterAdvanced`. Hash wajib agar
perubahan file referensi tidak lolos diam-diam. Perubahan command atau hash juga
mengubah fingerprint provider config dan mewajibkan asset plan baru.

### Bridge animation Wan 2.2

Bridge `scripts/providers/comfyui_animation.py` mengubah request asset
`animation` menjadi workflow native Wan 2.2 TI2V. Provider tidak menerima path
reference secara longgar. Ia mencari completed reference attempt milik shot yang
sama, mencocokkan `dependency_artifacts` dengan fingerprint output, menghitung
ulang SHA-256 file, lalu baru mengunggah gambar tersebut ke ComfyUI. Reference
yang hilang, berubah, berada di luar production root, atau tidak cocok dengan
lineage membuat generation gagal tertutup.

Health check Wan:

```powershell
& ".\.venv\Scripts\python.exe" scripts\providers\comfyui_animation.py `
  --server "http://127.0.0.1:8188" `
  --diffusion-model "wan2.2_ti2v_5B_fp16.safetensors" `
  --text-encoder "umt5_xxl_fp8_e4m3fn_scaled.safetensors" `
  --vae "wan2.2_vae.safetensors" `
  --generation-width 416 `
  --generation-height 736 `
  --max-frames 49 `
  --health-check
```
`generation-width` dan `generation-height` adalah resolusi inferensi yang harus
kelipatan 32. Setelah decode, `ImageScale` mengubah frame ke ukuran yang diminta
asset plan, misalnya 720×1280. Nilai 416×736 dan 49 frame adalah preset yang sudah
divalidasi pada RTX 4060 Laptop 8 GB dalam mode low-VRAM.

Wan memakai jumlah frame berpola `4n+1`. Bridge menghitung frame dari durasi dan
fps, membulatkannya ke pola tersebut, lalu membatasi dengan `--max-frames`.
Apabila request lebih panjang daripada batas, output menjadi lebih pendek dan
`duration_clamped` tercatat pada output JSON provider. Gunakan shot pendek,
naikkan batas setelah pengujian VRAM, atau tangani extension pada Checkpoint G;
jangan menganggap asset `COMPLETED` otomatis telah lolos pemeriksaan durasi.

Output diambil melalui `/view`, diperiksa sebagai container MP4, dan dipindahkan
secara atomik ke path attempt. Server selain loopback ditolak.

### Quick Video Agent

`scripts/video_agent.py` membuat satu video pendek vertikal dari sebuah ide
melalui pipeline ComfyUI lokal. Pipeline tidak menggunakan satu prompt generik
untuk semua tema. Setiap produksi memilih visual profile yang sesuai dengan
jenis konten.

Alur yang dijalankan:

1. Memuat visual profile dan delivery profile.
2. Memeriksa kesehatan provider reference, animation, dan finalization.
3. Membuat request gambar referensi dari ide.
4. Menghasilkan gambar referensi melalui SDXL.
5. Menghitung SHA-256 gambar referensi.
6. Melepas model reference dan membersihkan memori ComfyUI.
7. Mengikat gambar referensi sebagai dependency animasi.
8. Menghasilkan `video-source.mp4` melalui Wan 2.2.
9. Melakukan finalisasi ke format Shorts 720x1280 60 fps.
10. Menyimpan request, artifact, hash, status, health check, resource handoff,
    dan hasil probe di `run-manifest.json`.

Contoh penggunaan:

```powershell
& ".\.venv\Scripts\python.exe" `
  ".\scripts\video_agent.py" `
  --idea "A clean professional 3D visualization of a modern office" `
  --motion "The camera slowly moves forward while the office geometry remains stable." `
  --visual-style "corporate-3d" `
  --story-id "office-visual-test" `
  --production-id "office-visual-take-01" `
  --seed 20261109
```

Output produksi disimpan di:

```text
productions/quick-video/<production-id>/
├── reference-request.json
├── reference.png
├── animation-request.json
├── video-source.mp4
├── video.mp4
└── run-manifest.json
```

`video-source.mp4` merupakan hasil langsung provider animasi. `video.mp4`
merupakan artifact delivery yang telah melewati finalizer.

#### Visual profile

Konfigurasi visual tersedia di
`config/visual-style-profiles.json`. Profile awal yang tersedia:

- `neutral`
- `photoreal`
- `corporate-3d`
- `children-cartoon`

Visual profile mengatur checkpoint, sampler, scheduler, steps, CFG, positive
prompt suffix, negative prompt suffix, dan arahan animasi. Status
`development` berarti profile telah dapat diuji tetapi belum dijamin memenuhi
standar visual produksi untuk semua tema.

Profile merupakan arahan gaya, bukan pengganti perencanaan shot. Ide, subjek,
komposisi, pencahayaan, gerakan kamera, dan negative prompt tetap harus
disesuaikan dengan tujuan setiap video.

#### Delivery Shorts

Delivery profile utama adalah `shorts-720p60`:

- aspect ratio: 9:16
- resolution: 720x1280
- frame rate: 60 fps
- container: MP4
- video codec: H.264
- pixel format: yuv420p
- audio codec target: AAC

Wan 2.2 saat ini menghasilkan source pada 24 fps. Finalizer menggunakan FFmpeg
`minterpolate` untuk menghasilkan delivery 60 fps. Dengan demikian, output
60 fps saat ini merupakan hasil motion interpolation dan bukan generation
native 60 fps.

Audio hanya dipertahankan atau dikodekan apabila source memiliki audio. Quick
Video Agent saat ini belum mempunyai provider voice, music, atau SFX sehingga
hasil smoke test dapat memiliki `has_audio: false`.

#### Resource handoff

Setelah reference selesai, pipeline memanggil endpoint ComfyUI `/free` dengan:

```json
{
  "unload_models": true,
  "free_memory": true
}
```

Handoff ini melepaskan model SDXL sebelum Wan 2.2 dimuat. Hasilnya dicatat pada
`resource_handoffs` di manifest agar perpindahan provider dapat diaudit.

Pada mesin Windows dengan RAM 16 GB dan GPU 8 GB, jalankan ComfyUI dengan
`--disable-pinned-memory`. Pagefile yang memadai juga diperlukan untuk
mengurangi risiko kegagalan pembacaan host buffer ketika berpindah dari model
reference ke model animation.

#### Status dan quality gate

Status penting pada pipeline:

- `AWAITING_REFERENCE_QC`: reference tersedia dan menunggu pemeriksaan visual.
- `AWAITING_FINALIZATION`: source animation tersedia dan menunggu finalizer.
- `AWAITING_FINAL_QC`: video delivery tersedia dan menunggu pemeriksaan akhir.
- `FAILED`: salah satu tahap gagal dan informasi kegagalan dicatat.
- `COMPLETED`: hanya digunakan setelah artifact benar-benar disetujui.

Pipeline tidak menandai hasil sebagai `COMPLETED` hanya karena file berhasil
dibuat. Operator tetap harus memeriksa komposisi, anatomi atau geometri,
konsistensi subjek, gerakan, ghosting, flicker, durasi, audio, dan kelayakan
tayang.

#### Failure manifest

Jika pipeline gagal, `run-manifest.json` tetap ditulis dengan state `FAILED`.
Informasi kegagalan mencakup:

```json
{
  "failure": {
    "stage": "finalization",
    "error_type": "VideoFinalizeError",
    "message": "Ringkasan kesalahan",
    "occurred_at": "timestamp"
  }
}
```

Tahap kegagalan dapat berupa `health_check`, `reference_generation`,
`resource_cleanup`, `animation_generation`, atau `finalization`. Artifact yang
sudah berhasil dibuat tetap dicatat agar diagnosis dan retry tidak dimulai
tanpa konteks.

## 1. Voice cast

Buat JSON assignment, misalnya `productions/voice-assignments.json`:

```json
{
  "char-protagonist": {
    "provider": "local-tts",
    "provider_voice_id": "voice-ari-v1",
    "language": "id",
    "style": "hangat dan tenang"
  }
}
```

Lalu kunci cast untuk story revision yang telah disetujui:

```powershell
& ".\.venv\Scripts\python.exe" scripts\animation_studio.py cast `
  --story-manifest "stories\episode-001\revisions\REVISION_ID\story-manifest.json" `
  --assignments ".\productions\voice-assignments.json" `
  --output ".\productions\episode-001-voice-cast.json"
```

`--auto-fixture` hanya disediakan untuk smoke test offline.

## 2. Asset plan

```powershell
Copy-Item ".\config\asset-providers.example.json" `
  ".\config\asset-providers.local.json"

# Edit asset-providers.local.json agar menunjuk executable provider nyata.
& ".\.venv\Scripts\python.exe" scripts\animation_studio.py prepare `
  --story-manifest "stories\episode-001\revisions\REVISION_ID\story-manifest.json" `
  --voice-cast ".\productions\episode-001-voice-cast.json" `
  --provider command `
  --provider-config ".\config\asset-providers.local.json" `
  --production-id "episode-001-v1"
```

Asset plan lama memakai 720x1280 dan 30 fps sebagai setting generation.
Nilai tersebut bukan delivery profile final. Jalur Quick Video Agent
menghasilkan delivery Shorts 720x1280 60 fps melalui
`scripts/providers/video_finalize.py`. Integrasi finalizer ke jalur Supervisor
yang lama masih menjadi pekerjaan lanjutan.

## 3. Generation melalui Supervisor

```powershell
& ".\.venv\Scripts\python.exe" scripts\supervisor.py enqueue assets `
  --plan "PATH\TO\asset-plan.json"

& ".\.venv\Scripts\python.exe" scripts\supervisor.py worker --once
```

Task retry memakai ulang asset completed dan hanya mengulang asset gagal/missing.
API atau credential provider tidak disimpan oleh Supervisor.

## 4. Regenerasi satu shot

Gunakan ID baru yang bermakna. Contoh ini membuat reference dan animation take
baru untuk satu shot, tanpa mengulang voice, SFX, atau shot lain:

```powershell
& ".\.venv\Scripts\python.exe" scripts\supervisor.py enqueue assets `
  --plan "PATH\TO\asset-plan.json" `
  --shot shot-01 `
  --asset reference `
  --asset animation `
  --regeneration-id "shot-01-take-02"
```

Mengirim ulang `regeneration-id` yang sama bersifat idempotent. Gunakan ID baru
untuk take baru. Direct CLI memiliki `--force` untuk inspeksi operator, tetapi
Supervisor sengaja memakai regeneration ID agar retry tidak menggandakan output.

## 5. Validasi

```powershell
& ".\.venv\Scripts\python.exe" scripts\animation_studio.py validate `
  --plan "PATH\TO\asset-plan.json" `
  --require-complete
```

Tanpa `--require-complete`, manifest partial/failed dapat diperiksa selama semua
artifact yang tercatat masih valid. Dengan flag tersebut, hanya status
`COMPLETED` yang dapat masuk Checkpoint G.

## Batas checkpoint

Repository menyediakan provider reference ComfyUI, provider animasi Wan 2.2,
resource handoff, visual profile, dan finalizer Shorts untuk jalur Quick Video
Agent. Model weights, kebutuhan VRAM, pagefile, dan lisensi model tetap harus
dipilih serta diuji pada mesin operator. Pipeline belum memiliki voice
provider, SFX provider, music director, episode timeline, audio mixing,
lipsync, black-frame detection, automatic visual scoring, AI Office, atau
upload YouTube. Integrasi finalizer ke jalur Supervisor lama juga belum
dilakukan.
