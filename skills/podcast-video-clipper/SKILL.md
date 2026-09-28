---
name: podcast-video-clipper
description: Gunakan untuk mengubah link podcast atau video panjang menjadi klip vertikal profesional dengan motion semantik MotionCraft/Remotion yang mengikuti ucapan dan ruang kosong di sekitar subjek, cinematic focus, headline, subtitle, SFX, visual explainer, Campaign Mode, pemeriksaan kepatuhan, dan persetujuan Telegram sebelum publikasi.
---

# Podcast Video Clipper

## When to Use

Gunakan saat pengguna memberikan link YouTube atau link media lain dan meminta:

- memilih highlight podcast atau video panjang;
- membuat klip vertikal dengan hook, subtitle, motion, dan caption;
- memberi polesan cinematic gelap di sisi frame agar subjek lebih fokus;
- membuat headline pembuka yang akurat, mudah dibaca, dan menggantung;
- menambah spotlight text dan SFX pada bagian yang masih terasa mentah;
- menyisipkan diagram, perbandingan, angka, atau concept cutaway untuk memperjelas ucapan;
- mengirim semua hasil ke Telegram untuk ditinjau;
- menunggu persetujuan sebelum publikasi.

Jangan gunakan untuk mengunduh media berbayar, privat, dilindungi DRM, atau media yang tidak berhak diproses oleh pengguna.

## Fixed Paths

Gunakan executable dan orchestrator berikut:

```bash
PYTHON="D:/Hermes/video-agent/.venv/Scripts/python.exe"
CLIPPER="D:/Hermes/video-agent/scripts/podcast_clipper.py"
CAMPAIGN_ENGINE="D:/Hermes/video-agent/scripts/campaign_pro.py"
VISUAL_ENGINE="D:/Hermes/video-agent/scripts/visual_explainer_pro.py"
MOTIONCRAFT_BRIDGE="D:/Hermes/video-agent/scripts/motioncraft_bridge.py"
MOTIONCRAFT_RENDERER="D:/Hermes/video-agent/motioncraft-renderer"
```

Jangan mengganti atau menulis token Telegram ke file skill, rencana klip, log, atau jawaban chat. Token hanya boleh berada di `D:/Hermes/.env`.

Integrasi persetujuan memerlukan `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, dan
`TELEGRAM_OWNER_USER_ID` di file tersebut. Jangan meminta nilainya apabila
perintah `doctor` sudah menyatakan konfigurasi Telegram lengkap.

## Campaign Mode

Aktifkan Campaign Mode apabila pengguna memberikan briefing, aset unduhan,
aturan brand, poin wajib, CTA, disclaimer, atau syarat distribusi antar-klip.
Jangan mengandalkan ingatan atau prompt bebas setelah render dimulai. Ubah brief
menjadi `campaign.json` agar pipeline dapat menolak hasil yang tidak patuh.

### Membuat workspace campaign

```bash
"$PYTHON" "$CLIPPER" campaign init \
  --directory "D:/Hermes/video-agent/campaigns/NAMA_CAMPAIGN" \
  --name "NAMA CAMPAIGN"
```

Simpan briefing asli sebagai `brief.md`. Letakkan logo, CTA, bumper, musik,
video overlay, dan aset lain di folder `assets/`. Jika brief berasal dari PDF,
gambar, chat, atau dokumen lain, baca seluruh isinya lalu salin ringkasan faktual
ke `brief.md`; jangan menghilangkan larangan, ukuran, durasi, atau cakupan aset.

### Menerjemahkan brief

Isi `campaign.json` dengan:

- `visual_identity` untuk font dan warna brand;
- `required_components` untuk aset visual/audio;
- `content_requirements` untuk poin yang harus benar-benar ada dalam ucapan;
- `caption_rules` untuk teks dan hashtag wajib;
- `forbidden_phrases` untuk claim atau kata yang dilarang.

Gunakan `apply` berikut:

- `every_clip`: wajib pada semua klip;
- `clip_ids`: hanya pada ID klip yang ditentukan;
- `at_least_once`: minimal satu klip;
- `at_least_n_clips`: minimal sebanyak `minimum_clips`.

Jenis komponen yang didukung:

- `image_overlay`: logo, watermark, frame, CTA PNG/JPG/WebP;
- `video_overlay`: overlay video MP4/MOV/WebM;
- `audio`: musik atau audio campaign;
- `intro`: bumper sebelum isi klip;
- `outro`: bumper setelah isi klip.

Contoh aturan ringkas:

```json
{
  "schema_version": 1,
  "campaign_id": "kopi-01",
  "campaign_name": "Campaign Kopi",
  "brief_file": "brief.md",
  "visual_identity": {
    "font_name": "Montserrat SemiBold",
    "font_file": "assets/Montserrat-SemiBold.ttf",
    "primary_color": "F7F7F7",
    "outline_color": "111111"
  },
  "required_components": [
    {
      "id": "brand-logo",
      "type": "image_overlay",
      "file": "assets/logo.png",
      "apply": "every_clip",
      "position": "top-right",
      "width_percent": 16,
      "opacity": 0.9,
      "start": 0,
      "end": "full"
    },
    {
      "id": "cta-ending",
      "type": "image_overlay",
      "file": "assets/cta.png",
      "apply": "every_clip",
      "position": "bottom-center",
      "show_at": "ending",
      "duration": 2.5
    }
  ],
  "content_requirements": [
    {
      "id": "problem",
      "description": "Masalah utama audiens",
      "keywords": ["masalah", "kesulitan"],
      "match": "any",
      "apply": "at_least_once"
    }
  ],
  "caption_rules": {
    "required_text": ["Informasi lengkap cek link di bio"],
    "required_hashtags": ["#NamaCampaign"]
  },
  "forbidden_phrases": ["dijamin berhasil"]
}
```

Path aset relatif dihitung dari lokasi `campaign.json`. Jangan mengubah kata
kunci agar cocok dengan klip yang sebenarnya tidak membahas poin tersebut.
Warna `visual_identity` menggunakan format hex `RRGGBB`. Jika memakai font file,
isi `font_name` dengan nama keluarga font yang benar dan simpan TTF/OTF/TTC di
folder assets. Pipeline akan memasukkan folder font ke renderer subtitle.
Apabila brief ambigu atau aset wajib tidak tersedia, hentikan sebelum render dan
minta pengguna melengkapinya.

## Procedure

### 1. Periksa pipeline

Jalankan sebelum pekerjaan pertama atau ketika lingkungan berubah:

```bash
"$PYTHON" "$CLIPPER" doctor
```

Hentikan dan laporkan komponen yang gagal. Jangan melanjutkan render jika pemeriksaan tidak lulus.

### 2. Pastikan link dapat diproses

Terima hanya URL `http` atau `https`. Gunakan satu video, bukan playlist. Jangan mencoba melewati login, paywall, pembatasan wilayah, atau DRM.

### 3. Unduh dan transkripsikan

Buat job ID yang singkat dan unik, lalu jalankan:

```bash
"$PYTHON" "$CLIPPER" ingest "URL_VIDEO" --job "JOB_ID" --model medium --language id
```

Catat path `Transcript` dan `Template rencana` dari output. Unduhan yang terputus boleh dilanjutkan dengan menjalankan kembali perintah yang sama. Jangan memakai `--force-download` kecuali pengguna meminta pengunduhan ulang.

### 4. Tinjau dan koreksi transkrip

Baca `transcript-full.json`. Perbaiki typo hanya jika konteksnya kuat. Pertahankan timestamp dan jangan mengubah makna ucapan. Untuk koreksi kata bertimestamp, perbarui `words[].word` serta teks segmennya agar subtitle memakai bentuk yang benar.

Jangan mengarang kalimat yang tidak diucapkan. Tandai bagian ambigu sebagai catatan, bukan menebaknya secara agresif.

### 5. Pilih highlight

Pilih 3-5 klip terbaik secara default. Gunakan kriteria berikut:

1. Utamakan gagasan yang dapat dipahami tanpa konteks panjang.
2. Mulai pada batas kalimat atau tepat sebelum premis penting.
3. Akhiri setelah jawaban, kejutan, atau payoff selesai.
4. Targetkan 20-60 detik; rentang teknis yang diizinkan 8-90 detik.
5. Hindari klip yang mengandung klaim berbahaya tanpa konteks.
6. Variasikan topik; jangan memilih beberapa klip yang menyampaikan poin sama.
7. Tandai punchline yang layak mendapat spotlight, tetapi jangan memberi efek pada setiap kalimat.

### 6. Isi clip-plan.json

Edit template rencana yang dibuat pada folder job. Gunakan struktur berikut:

```json
{
  "schema_version": 1,
  "job_id": "JOB_ID",
  "source_url": "URL_VIDEO",
  "source_path": "PATH_DARI_TEMPLATE",
  "transcript_path": "PATH_DARI_TEMPLATE",
  "source_title": "Judul sumber",
  "campaign_path": "D:/Hermes/video-agent/campaigns/NAMA_CAMPAIGN/campaign.json",
  "clips": [
    {
      "id": "clip-01",
      "start": 125.4,
      "end": 164.8,
      "title": "Judul klip yang natural",
      "hook": "KENAPA INI BISA TERJADI?",
      "headline": {
        "kicker": "BIKIN SEDIH...",
        "text": "RADITYA DIKA BARU SADAR SOAL INI",
        "subject": "RADITYA DIKA",
        "highlight": "RADITYA DIKA",
        "duration": 4.0,
        "open_loop": true
      },
      "caption": "Caption singkat yang menambah konteks tanpa clickbait palsu.",
      "hashtags": ["podcast", "edukasi", "insight"],
      "focus": "auto",
      "cinematic_focus": "dramatic",
      "cinematic_side": "both",
      "editorial_style": "balanced",
      "visual_style": "immersive",
      "motioncraft": {
        "mode": "auto",
        "strict_transcript": false
      },
      "brief_points": ["problem"]
    }
  ]
}
```

Aturan field:

- `id`: unik dalam satu job; gunakan `clip-01`, `clip-02`, dan seterusnya.
- `start` dan `end`: detik pada video sumber.
- `hook`: 3-8 kata ideal, maksimal 12 kata, akurat terhadap isi klip.
- `headline.kicker`: label 1-5 kata seperti `BIKIN SEDIH...`, `VIRAL...`, atau `TERNYATA...`; gunakan hanya jika nada dan fakta footage mendukungnya.
- `headline.text`: maksimal 16 kata, 2-3 baris saat dirender, memuat inti klip dan dibuat open-loop tanpa mengubah makna.
- `headline.subject`: nama host, bintang tamu, atau tokoh yang terverifikasi dari judul, metadata, atau ucapan. Jangan menebak identitas.
- `headline.highlight`: frasa yang harus ada persis dalam `headline.text`; prioritaskan nama tokoh agar tampil dengan font dan warna aksen.
- `headline.duration`: gunakan 3,0-5,0 detik. Default 3,8 detik; pilih sekitar 4 detik agar sempat dibaca.
- `headline.open_loop`: gunakan `true` agar headline yang bukan pertanyaan diakhiri elipsis dan terasa menggantung.
- `caption`: natural, ringkas, dan tidak mengulang seluruh subtitle.
- `hashtags`: 3-5 tag relevan; jangan memakai tag yang menyesatkan.
- `focus`: gunakan `auto`; gunakan `left`, `center`, atau `right` hanya jika framing otomatis keliru.
- `cinematic_focus`: gunakan `dramatic` untuk matte gelap bertingkat, vignette, dan grade kontras; `subtle` untuk hasil lebih ringan; `off` hanya jika source sudah memiliki treatment setara.
- `cinematic_side`: gunakan `both` untuk fokus simetris, `left` atau `right` jika hanya satu sisi perlu ditutup, atau `auto` agar sisi berlawanan dari posisi subjek dipilih.
- `editorial_style`: gunakan `subtle`, `balanced`, `energetic`, atau `off`. Gunakan `balanced` secara default.
- `visual_style`: gunakan `subtle`, `balanced`, `immersive`, atau `off`. Gunakan `immersive` jika pengguna meminta gaya visual seperti video referensi; gunakan `balanced` untuk hasil umum.
- `motioncraft.mode`: gunakan `auto` secara default, `on` untuk hard requirement, atau `off` untuk renderer FFmpeg lama. Mode `auto` memakai MotionCraft jika renderer sehat dan jatuh kembali ke FFmpeg apabila renderer tidak tersedia.
- `motioncraft.strict_transcript`: gunakan `true` jika setiap kata berprobabilitas rendah harus menghentikan render. Default `false` tetap membuat daftar peringatan di `motion-plan.json` tanpa mengarang koreksi.
- `campaign_path`: hapus field ini untuk pekerjaan non-campaign; gunakan path absolut atau path relatif terhadap `clip-plan.json` untuk Campaign Mode.
- `brief_points`: daftar ID poin brief yang memang ditemukan dalam rentang ucapan klip tersebut. Jangan menandai poin hanya karena dibutuhkan campaign.

Biarkan analyzer membuat `editorial_events` secara otomatis apabila agent belum melihat frame hasil crop. Analyzer memeriksa pergantian scene, motion, indikasi overlay, energi suara, dan isi transkrip. Ia memprioritaskan punchline pada bagian yang terlihat belum diedit serta menghindari bagian yang sudah padat efek.

### Headline dan cinematic focus

Tulis headline setelah membaca ucapan dalam rentang klip, bukan hanya judul
video. Gunakan struktur dua tingkat: `kicker` sebagai pemancing singkat dan
`text` sebagai janji informasi yang belum menutup payoff. Sertakan nama host
atau bintang tamu bila identitasnya pasti dan relevan dengan isi klip. Letakkan
nama tersebut di `highlight` agar renderer memakai bobot font lebih tebal dan
warna aksen.

Headline harus tetap benar jika dibaca tanpa caption. Jangan memakai `VIRAL`,
`BIKIN SEDIH`, `MENGEJUTKAN`, atau kata emosional lain bila footage tidak
mendukungnya. Jangan menulis hasil atau kesimpulan lengkap di headline; sisakan
pertanyaan atau konsekuensi yang baru terjawab setelah penonton melanjutkan.

Renderer mempertahankan headline selama 3-5 detik, memilih area atas atau bawah
yang tidak menutup wajah, memindahkan subtitle awal ke area alternatif, lalu
menjaga 5,2 detik pertama bebas dari spotlight dan visual cutaway. Cinematic
focus dirender sebelum headline/subtitle, sehingga matte hitam kiri-kanan tidak
menurunkan keterbacaan teks.

Jika analisis agent memiliki alasan kuat untuk mengatur event sendiri, tambahkan daftar berikut pada entry klip. Timestamp bersifat relatif terhadap awal klip, bukan video sumber:

```json
"editorial_events": [
  {
    "start": 6.2,
    "end": 7.5,
    "text": "KEBANYAKAN KAFEIN",
    "animation": "impact",
    "position": "auto",
    "sfx": "impact",
    "sfx_gain_db": -19
  }
]
```

Pilihan animasi: `impact`, `pop`, `slide`, atau `soft_pop`. Pilihan SFX: `impact`, `pop`, `whoosh`, atau `click`. Batasi maksimal tiga event untuk klip sekitar 30 detik dan beri jarak minimal empat detik. Pertahankan SFX antara -24 hingga -16 dB agar suara pembicara tetap dominan.

### Visual Explainer untuk footage plain

Visual explainer berbeda dari spotlight text. Spotlight hanya menekankan kata,
sedangkan visual explainer mengganti atau menutupi sementara talking-head dengan
motion graphic yang menerangkan maksud pembicara. Analyzer hanya memilih rentang
yang memiliki sedikit cut, motion, dan overlay bawaan.

Jenis visual yang didukung:

- `concept`: menggambarkan satu ide inti dengan node dan kata kunci;
- `process`: menampilkan alur maksimal tiga langkah;
- `comparison`: membandingkan dua konsep;
- `metric`: menonjolkan angka/fakta beserta konteksnya.

Aturan editorial:

1. Gunakan maksimal satu sampai tiga visual explainer per klip, sesuai durasi.
2. Beri jarak sekitar 6,5-8 detik antarevent.
3. Jangan menambahkan cutaway pada opening headline 5,2 detik pertama.
4. Jangan menumpuk visual di rentang yang sudah memiliki overlay atau pergantian scene padat.
5. Teks visual harus merangkum ucapan yang benar-benar ada; jangan menambah claim.
6. Subtitle dirender setelah visual agar tetap terbaca di safe area.
7. SFX cutaway berada sekitar -23 sampai -20 dB dan tidak boleh menutupi suara.

Biarkan analyzer mengisi `visual-plan.json` secara otomatis. Jika agent perlu
mengatur sendiri berdasarkan pemahaman brief atau transkrip, gunakan timestamp
relatif terhadap awal klip:

```json
"visual_events": [
  {
    "start": 9.4,
    "end": 13.0,
    "type": "process",
    "title": "CARA KERJANYA",
    "items": ["INPUT", "ANALISIS", "HASIL"],
    "treatment": "cutaway",
    "animation": "slide",
    "sfx": "whoosh",
    "sfx_gain_db": -22
  }
]
```

Gunakan `asset_path` opsional jika brief menyediakan ilustrasi PNG/JPG/WebP
sendiri atau gambar AI yang sudah dibuat. Path relatif dihitung dari folder job.
Tanpa `asset_path`, engine membuat motion graphic lokal secara otomatis. Untuk
revisi, ubah hanya event yang bermasalah lalu render ulang klip terkait.

### MotionCraft semantik dan subject-aware

Jangan meminta MotionCraft menebak isi ucapan dari video untuk kedua kalinya.
Hermes adalah sumber kebenaran untuk transkrip dan pilihan editorial. Bridge
menggabungkan `transcript.json`, `editorial-plan.json`, `visual-plan.json`, dan
`subtitle.analysis.json` menjadi `motion-plan.json` dengan timestamp relatif
terhadap klip.

Aturan integrasi:

1. Kaitkan motion ke intent ucapan: pertanyaan, peringatan, kontras, urutan,
   angka, reveal, atau penjelasan. Jangan memberi animasi generik pada setiap kata.
2. Gunakan timestamp kata sebagai anchor jika tersedia; gunakan awal segmen hanya
   sebagai fallback.
3. Gunakan face tracking untuk memilih `left`, `right`, `top`, atau `bottom`
   sebagai ruang bebas. Hindari zona subtitle aktif.
4. Pertahankan 5,2 detik pertama untuk headline. Jangan menambah cue semantik atau
   visual cutaway pada rentang tersebut.
5. Animasikan aset dari `visual-plan.json` di Remotion. Subtitle dan spotlight
   tetap dirender sesudahnya oleh ASS agar keterbacaan dan Campaign Mode tidak berubah.
6. Setelah MotionCraft melakukan zoom/push, kosongkan `video_motion` lama pada
   edit plan agar tidak terjadi double zoom.
7. Simpan kata berprobabilitas rendah di `motion-plan.json > qa` dan jangan
   mengubahnya tanpa konteks yang kuat.

Untuk memeriksa bridge secara langsung:

```bash
"$PYTHON" "$MOTIONCRAFT_BRIDGE" doctor --renderer "$MOTIONCRAFT_RENDERER"
```

### 7. Validasi rencana

```bash
"$PYTHON" "$CLIPPER" validate --plan "PATH_CLIP_PLAN"
```

Perbaiki semua error sebelum render.

Dalam Campaign Mode, validasi juga membuat `campaign-assignment.json` dan
`campaign-preflight.json`. Jangan render jika status preflight bukan `passed`.
Gunakan pemeriksaan eksplisit berikut jika diperlukan:

```bash
"$PYTHON" "$CLIPPER" campaign check --plan "PATH_CLIP_PLAN"
```

### 8. Aktifkan listener persetujuan

```bash
"$PYTHON" "$CLIPPER" listener start
```

Jangan memulai listener kedua jika statusnya sudah aktif. Jika muncul konflik `getUpdates`, hentikan proses polling bot lain atau gunakan bot persetujuan khusus.

### 9. Render dan kirim semua hasil

```bash
"$PYTHON" "$CLIPPER" render --plan "PATH_CLIP_PLAN" --continue-on-error
```

Pipeline akan melakukan crop vertikal berbasis wajah, membuat motion plan dari makna ucapan, merender cue dan visual explainer dengan MotionCraft/Remotion di ruang bebas sekitar subjek, memberi cinematic focus matte, membuat headline dan subtitle dinamis, menambahkan spotlight text serta SFX halus, menormalkan audio, memasang aset campaign, membuat caption, menjalankan compliance gate, lalu mengirim hasil yang lulus ke Telegram.

Periksa `editorial-plan.json` pada folder setiap klip. Jika klasifikasinya `already_edited`, gunakan event lebih sedikit atau kosong. Jangan menumpuk spotlight di atas teks yang sudah ada.

Periksa juga `visual-plan.json` dan folder `visual-assets`. Setiap event harus
memiliki alasan `plainness`, tipe visual yang sesuai dengan ucapan, serta aset
yang dapat dibuka. Jika visual terasa dekoratif tetapi tidak membantu pemahaman,
hapus event tersebut; kualitas lebih penting daripada jumlah efek.

Dalam Campaign Mode, periksa pula `campaign-applied.json` dan
`campaign-compliance.json`. Jika satu syarat wajib gagal, jangan mengirim atau
mengunggah klip tersebut. Perbaiki konfigurasi, pemilihan rentang, caption, atau
aset lalu render ulang hanya klip terkait.

Laporkan jumlah klip berhasil dan gagal. Berikan folder job dan manifest, tetapi jangan menyatakan bahwa klip telah dipublikasikan.

### 10. Pantau keputusan

Gunakan:

```bash
"$PYTHON" "$CLIPPER" status --job "JOB_ID"
```

Makna status:

- `pending`: menunggu pengguna.
- `approved`: boleh diteruskan ke uploader platform yang sudah dikonfigurasi.
- `rejected`: jangan unggah.
- `revision_requested`: revisi hook, caption, batas waktu, framing, subtitle, atau motion sesuai arahan pengguna.

### 11. Revisi satu klip

Perbarui hanya entry terkait di `clip-plan.json`, lalu jalankan:

```bash
"$PYTHON" "$CLIPPER" render --plan "PATH_CLIP_PLAN" --clip "clip-01"
```

Hasil revisi kembali berstatus menunggu persetujuan. Jangan menganggap persetujuan versi lama berlaku untuk versi baru.

## Publication Gate

Jangan pernah mengunggah klip dengan status selain `approved`. Persetujuan berlaku untuk file hasil render tertentu, bukan untuk seluruh job atau versi revisi berikutnya.

Jika uploader platform belum dikonfigurasi, berhenti setelah persetujuan dan tanyakan platform tujuan. Jangan meminta password di chat. Gunakan OAuth, token lokal, atau mekanisme autentikasi resmi platform.

## Pitfalls

- `409 Conflict` dari Telegram berarti ada lebih dari satu proses polling untuk token yang sama.
- Hasil crop yang salah dapat diperbaiki dengan `focus: left`, `center`, atau `right`.
- Timestamp yang memotong kata menghasilkan subtitle janggal; sesuaikan ke batas kata/kalimat.
- Klip yang gagal dikirim tetap dapat ditemukan di `clips/<clip-id>/clip-final.mp4`.
- SFX harus mendukung ucapan, bukan menutupinya. Jika terasa mengganggu, turunkan `sfx_gain_db` atau gunakan `editorial_style: subtle`.
- Jangan menganggap semua footage plain membutuhkan animasi. Visual explainer hanya dipakai ketika ada konsep, proses, perbandingan, atau angka yang menjadi lebih jelas secara visual.
- Jangan menggunakan ilustrasi yang bertentangan dengan ucapan, mengubah identitas orang, atau menyajikan visual sintetis sebagai bukti faktual.
- Aset campaign yang dijadwalkan `every_clip` tidak boleh hilang pada satu klip pun.
- Poin brief harus diverifikasi dari ucapan sumber melalui keywords; jangan membuat ucapan, testimoni, atau claim baru.
- `campaign-preflight.json` yang gagal adalah hard stop. Jangan melewatinya dengan menghapus requirement tanpa persetujuan pengguna.
- Jangan memakai efek untuk mengubah makna, membuat kutipan palsu, atau memberi dramatisasi yang bertentangan dengan konteks.
- Jangan menjalankan ulang seluruh job untuk satu revisi; gunakan `--clip`.
- Jangan menjalankan `--force-download` atau menghapus folder job tanpa permintaan pengguna.

## Verification

Pekerjaan berhasil ketika:

1. `doctor` lulus.
2. `validate` menyatakan rencana valid.
3. `render-manifest.json` mencatat klip yang selesai.
4. Setiap folder klip memiliki `editorial-plan.json`, `visual-plan.json`, `visual-assets`, `motion-plan.json`, `clip-motioncraft.mp4`, dan `subtitle-editorial.ass` ketika MotionCraft aktif.
5. Spotlight text serta visual explainer tidak bertabrakan dengan subtitle dan SFX tidak menutupi suara.
6. Headline tampil 3-5 detik, mudah dibaca, sesuai footage, open-loop, dan nama tokoh yang terverifikasi mendapat highlight.
7. Cinematic focus menggelapkan sisi frame tanpa menutup wajah atau teks.
8. Visual explainer hanya muncul pada rentang plain dan benar-benar sesuai dengan ucapan.
9. Dalam Campaign Mode, preflight berstatus `passed` dan setiap klip memiliki `campaign-applied.json` serta `campaign-compliance.json` berstatus `passed`.
10. Semua komponen wajib dan cakupan poin brief terpenuhi tanpa membuat claim baru.
11. Semua klip yang lulus berhasil muncul di Telegram dengan tiga tombol keputusan.
12. `status --job` menunjukkan keputusan yang sama dengan tombol Telegram.
13. Tidak ada publikasi sebelum status `approved`.
14. `motion-plan.json` hanya berisi cue yang berasal dari transkrip, mencatat warning kata berprobabilitas rendah, dan tidak menumpuk motion pada visual cutaway.
15. `render-manifest.json` mencatat `motioncraft_rendered: true`; jika mode `auto` memakai fallback, alasannya tercatat pada `motioncraft_error`.
