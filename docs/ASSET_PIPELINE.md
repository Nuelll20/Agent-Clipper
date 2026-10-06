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
`reference`. Tiga provider lain sengaja tetap berupa placeholder sampai provider
video, TTS, dan SFX nyata dipasang. Karena itu, konfigurasi ini aman dipakai lebih
dahulu dengan generation selektif `--asset reference`; jangan menjalankan semua
kind sebelum ketiga provider lain siap.

IP-Adapter bersifat opsional. Tambahkan argumen berikut pada command `reference`:

```json
"--reference-image", "E:\\path\\to\\immutable-reference.png",
"--reference-sha256", "HEX_SHA256_TANPA_PREFIX"
```

Bridge mengunggah file ke input ComfyUI dengan nama berbasis hash, memverifikasi
hash sebelum setiap generation, lalu memakai `IPAdapterAdvanced`. Hash wajib agar
perubahan file referensi tidak lolos diam-diam. Perubahan command atau hash juga
mengubah fingerprint provider config dan mewajibkan asset plan baru.

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

Output menunjukkan path `asset-plan.json`. Plan default memakai 720×1280 dan
30 fps; dapat diubah dengan `--width`, `--height`, dan `--fps` sebelum generation.

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

Repository menyediakan kontrak provider dan command bridge, bukan bundel model
ComfyUI/TTS/video tertentu. Provider nyata, model weights, kebutuhan VRAM, dan
lisensi model harus dipilih serta diuji di laptop pengguna. Belum ada episode
timeline, mixing audio, encoder final, black-frame detection, lipsync, background
music director, AI Office, atau upload YouTube.
