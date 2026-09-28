import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODEL_ROOT = PROJECT_ROOT / "models" / "faster-whisper"

MODEL_ROOT.mkdir(parents=True, exist_ok=True)

os.environ.setdefault(
    "HF_HOME",
    str(PROJECT_ROOT / "models" / "huggingface")
)
os.environ.setdefault(
    "HUGGINGFACE_HUB_CACHE",
    str(PROJECT_ROOT / "models" / "huggingface" / "hub")
)

from faster_whisper import WhisperModel


def clean_number(value):
    if value is None:
        return None

    return round(float(value), 3)


def load_model(model_name):
    print(f"Memuat model Whisper: {model_name}")
    print(f"Lokasi model: {MODEL_ROOT}")
    print("Mode: CPU int8 (stabil untuk pengujian awal)")

    return WhisperModel(
        model_name,
        device="cpu",
        compute_type="int8",
        download_root=str(MODEL_ROOT)
    )


def transcribe_video(
    input_path,
    output_path,
    model_name,
    language
):
    input_path = Path(input_path).resolve()
    output_path = Path(output_path).resolve()

    if not input_path.exists():
        raise FileNotFoundError(
            f"Video tidak ditemukan: {input_path}"
        )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    model = load_model(model_name)

    selected_language = None
    if language.lower() != "auto":
        selected_language = language.lower()

    print(f"Memproses video: {input_path.name}")
    print("Membuat timestamp setiap kata...")

    segments_generator, info = model.transcribe(
        str(input_path),
        language=selected_language,
        beam_size=5,
        best_of=5,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={
            "min_silence_duration_ms": 350,
            "speech_pad_ms": 180
        },
        condition_on_previous_text=True,
        initial_prompt=(
            "Transkripsi percakapan podcast dalam bahasa Indonesia. "
            "Gunakan ejaan, nama, istilah, dan tanda baca yang tepat."
        )
    )

    output_segments = []
    full_text_parts = []
    total_words = 0

    for index, segment in enumerate(
        segments_generator,
        start=1
    ):
        words = []

        for word_data in segment.words or []:
            word_text = (word_data.word or "").strip()

            if not word_text:
                continue

            words.append({
                "word": word_text,
                "start": clean_number(word_data.start),
                "end": clean_number(word_data.end),
                "probability": round(
                    float(word_data.probability or 0.0),
                    4
                )
            })

        segment_text = (segment.text or "").strip()

        output_segments.append({
            "id": index,
            "start": clean_number(segment.start),
            "end": clean_number(segment.end),
            "text_original": segment_text,
            "text_corrected": None,
            "words": words,
            "avg_logprob": round(
                float(segment.avg_logprob or 0.0),
                4
            ),
            "no_speech_probability": round(
                float(
                    segment.no_speech_prob or 0.0
                ),
                4
            )
        })

        full_text_parts.append(segment_text)
        total_words += len(words)

        print(
            f"[{index}] "
            f"{segment.start:.2f}s - "
            f"{segment.end:.2f}s | "
            f"{segment_text}"
        )

    detected_language = getattr(
        info,
        "language",
        selected_language
    )

    language_probability = getattr(
        info,
        "language_probability",
        None
    )

    result = {
        "schema_version": 2,
        "generator": "video-agent-pro-transcriber",
        "model": model_name,
        "language": detected_language,
        "language_probability": (
            round(float(language_probability), 4)
            if language_probability is not None
            else None
        ),
        "duration": clean_number(
            getattr(info, "duration", None)
        ),
        "total_segments": len(output_segments),
        "total_words": total_words,
        "full_text_original": " ".join(
            full_text_parts
        ).strip(),
        "segments": output_segments
    }

    with output_path.open(
        "w",
        encoding="utf-8"
    ) as output_file:
        json.dump(
            result,
            output_file,
            ensure_ascii=False,
            indent=2
        )

    text_output_path = output_path.with_suffix(
        ".txt"
    )

    text_output_path.write_text(
        result["full_text_original"],
        encoding="utf-8"
    )

    print("")
    print("Transkripsi profesional selesai.")
    print(f"Bahasa: {detected_language}")
    print(f"Jumlah segmen: {len(output_segments)}")
    print(f"Jumlah kata: {total_words}")
    print(f"JSON: {output_path}")
    print(f"Teks: {text_output_path}")

    if total_words == 0:
        print("")
        print(
            "PERINGATAN: timestamp kata tidak dihasilkan."
        )
        sys.exit(2)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Membuat transcript podcast dengan "
            "timestamp setiap kata."
        )
    )

    parser.add_argument(
        "input_video",
        help="Lokasi video yang akan ditranskripsi"
    )
    parser.add_argument(
        "output_json",
        help="Lokasi output JSON"
    )
    parser.add_argument(
        "--model",
        default="medium",
        help="Model Whisper, default: medium"
    )
    parser.add_argument(
        "--language",
        default="id",
        help="Kode bahasa atau auto, default: id"
    )

    args = parser.parse_args()

    transcribe_video(
        input_path=args.input_video,
        output_path=args.output_json,
        model_name=args.model,
        language=args.language
    )


if __name__ == "__main__":
    main()
