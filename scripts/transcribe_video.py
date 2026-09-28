from pathlib import Path
import json
import sys

from faster_whisper import WhisperModel


def srt_timestamp(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1_000)

    return (
        f"{hours:02d}:{minutes:02d}:{seconds:02d},"
        f"{milliseconds:03d}"
    )


if len(sys.argv) < 2:
    raise SystemExit(
        "Pemakaian: python transcribe_video.py <video> [output_folder]"
    )

video_path = Path(sys.argv[1]).resolve()

if not video_path.exists():
    raise SystemExit(f"Video tidak ditemukan: {video_path}")

if len(sys.argv) >= 3:
    output_folder = Path(sys.argv[2]).resolve()
else:
    output_folder = video_path.parent

output_folder.mkdir(parents=True, exist_ok=True)

model_cache = Path(r"D:\Hermes\cache\whisper")
model_cache.mkdir(parents=True, exist_ok=True)

print("Memuat faster-whisper...")
print("Unduhan model hanya terjadi pada pemakaian pertama.")

model = WhisperModel(
    "base",
    device="cpu",
    compute_type="int8",
    download_root=str(model_cache),
)

print(f"Mulai mentranskripsikan: {video_path.name}")

segment_generator, info = model.transcribe(
    str(video_path),
    beam_size=5,
    vad_filter=True,
    word_timestamps=True,
)

segments = list(segment_generator)

transcript_data = {
    "source": str(video_path),
    "language": info.language,
    "language_probability": info.language_probability,
    "duration": info.duration,
    "segments": [],
}

srt_lines = []

for number, segment in enumerate(segments, start=1):
    text = segment.text.strip()

    transcript_data["segments"].append({
        "id": number,
        "start": round(segment.start, 3),
        "end": round(segment.end, 3),
        "text": text,
    })

    srt_lines.extend([
        str(number),
        (
            f"{srt_timestamp(segment.start)} --> "
            f"{srt_timestamp(segment.end)}"
        ),
        text,
        "",
    ])

json_path = output_folder / "transcript.json"
srt_path = output_folder / "subtitle.srt"
text_path = output_folder / "transcript.txt"

json_path.write_text(
    json.dumps(transcript_data, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

srt_path.write_text(
    "\n".join(srt_lines),
    encoding="utf-8",
)

text_path.write_text(
    "\n".join(item["text"] for item in transcript_data["segments"]),
    encoding="utf-8",
)

print("")
print("Transkripsi selesai.")
print(f"Bahasa terdeteksi : {info.language}")
print(f"Probabilitas      : {info.language_probability:.2%}")
print(f"Jumlah segmen     : {len(segments)}")
print(f"Transcript JSON   : {json_path}")
print(f"Subtitle SRT      : {srt_path}")
print(f"Transcript teks   : {text_path}")
