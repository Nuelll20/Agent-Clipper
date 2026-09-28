from pathlib import Path
import json
import math
import re
import sys

import cv2
import numpy as np


PLAY_RES_X = 720
PLAY_RES_Y = 1280
MAX_WORDS = 6


def ass_time(seconds):
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60

    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def ass_escape(text):
    return (
        str(text)
        .replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .strip()
    )


def wrap_phrase(text, maximum_length=28):
    text = " ".join(text.split())

    if len(text) <= maximum_length:
        return text

    words = text.split()

    if len(words) < 4:
        return text

    midpoint = len(text) / 2
    best_index = 1
    current_length = 0
    smallest_difference = float("inf")

    for index, word in enumerate(words[:-1], start=1):
        current_length += len(word) + 1
        difference = abs(current_length - midpoint)

        if difference < smallest_difference:
            smallest_difference = difference
            best_index = index

    first_line = " ".join(words[:best_index])
    second_line = " ".join(words[best_index:])

    return first_line + r"\N" + second_line


def split_text_into_groups(text):
    words = text.strip().split()
    groups = []
    current = []

    for word in words:
        current.append(word)

        punctuation_break = (
            len(current) >= 3
            and re.search(r"[,.!?;:]$", word)
        )

        if len(current) >= MAX_WORDS or punctuation_break:
            groups.append(current)
            current = []

    if current:
        groups.append(current)

    return groups


def build_caption_chunks(transcript):
    chunks = []

    for segment in transcript.get("segments", []):
        start = float(segment["start"])
        end = float(segment["end"])
        text = segment.get("text", "").strip()

        if not text or end <= start:
            continue

        timestamped_words = segment.get("words") or []

        if timestamped_words:
            group = []

            for word_data in timestamped_words:
                group.append(word_data)

                word_text = str(word_data.get("word", "")).strip()
                punctuation_break = (
                    len(group) >= 3
                    and re.search(r"[,.!?;:]$", word_text)
                )

                if len(group) >= MAX_WORDS or punctuation_break:
                    chunks.append({
                        "start": float(group[0]["start"]),
                        "end": float(group[-1]["end"]),
                        "text": " ".join(
                            str(item["word"]).strip()
                            for item in group
                        ),
                    })
                    group = []

            if group:
                chunks.append({
                    "start": float(group[0]["start"]),
                    "end": float(group[-1]["end"]),
                    "text": " ".join(
                        str(item["word"]).strip()
                        for item in group
                    ),
                })

            continue

        groups = split_text_into_groups(text)
        total_words = sum(len(group) for group in groups)
        duration = end - start
        elapsed_words = 0

        for group in groups:
            group_start = start + (
                duration * elapsed_words / total_words
            )

            elapsed_words += len(group)

            group_end = start + (
                duration * elapsed_words / total_words
            )

            chunks.append({
                "start": group_start,
                "end": group_end,
                "text": " ".join(group),
            })

    return chunks


def overlap_ratio(rectangle, face):
    rx1, ry1, rx2, ry2 = rectangle
    fx, fy, fw, fh = face
    fx2 = fx + fw
    fy2 = fy + fh

    left = max(rx1, fx)
    top = max(ry1, fy)
    right = min(rx2, fx2)
    bottom = min(ry2, fy2)

    if right <= left or bottom <= top:
        return 0.0

    intersection = (right - left) * (bottom - top)
    rectangle_area = max(1, (rx2 - rx1) * (ry2 - ry1))

    return intersection / rectangle_area


class FrameAnalyzer:
    def __init__(self, video_path):
        self.capture = cv2.VideoCapture(str(video_path))

        if not self.capture.isOpened():
            raise RuntimeError(f"Tidak dapat membuka video: {video_path}")

        self.width = int(
            self.capture.get(cv2.CAP_PROP_FRAME_WIDTH)
        )
        self.height = int(
            self.capture.get(cv2.CAP_PROP_FRAME_HEIGHT)
        )

        cascade_path = (
            Path(cv2.data.haarcascades)
            / "haarcascade_frontalface_default.xml"
        )

        self.face_detector = cv2.CascadeClassifier(
            str(cascade_path)
        )

        self.candidates = [
            {"name": "atas", "y_ratio": 0.25},
            {"name": "tengah", "y_ratio": 0.50},
            {"name": "bawah", "y_ratio": 0.76},
        ]

        self.previous_candidate = None

    def read_frame(self, timestamp):
        self.capture.set(
            cv2.CAP_PROP_POS_MSEC,
            max(0.0, timestamp) * 1000,
        )

        success, frame = self.capture.read()

        if not success:
            return None

        return frame

    def candidate_rectangle(self, candidate):
        center_x = self.width // 2
        center_y = int(
            self.height * candidate["y_ratio"]
        )

        rectangle_width = int(self.width * 0.84)
        rectangle_height = int(self.height * 0.18)

        x1 = max(0, center_x - rectangle_width // 2)
        y1 = max(0, center_y - rectangle_height // 2)
        x2 = min(self.width, center_x + rectangle_width // 2)
        y2 = min(self.height, center_y + rectangle_height // 2)

        return x1, y1, x2, y2

    def analyze(self, start, end):
        duration = max(0.2, end - start)

        sample_times = np.linspace(
            start + duration * 0.15,
            end - duration * 0.15,
            5,
        )

        measurements = {
            item["name"]: {
                "scores": [],
                "brightness": [],
            }
            for item in self.candidates
        }

        for timestamp in sample_times:
            frame = self.read_frame(float(timestamp))

            if frame is None:
                continue

            gray = cv2.cvtColor(
                frame,
                cv2.COLOR_BGR2GRAY,
            )

            faces = self.face_detector.detectMultiScale(
                gray,
                scaleFactor=1.12,
                minNeighbors=5,
                minSize=(45, 45),
            )

            for candidate in self.candidates:
                rectangle = self.candidate_rectangle(candidate)
                x1, y1, x2, y2 = rectangle
                region = gray[y1:y2, x1:x2]

                if region.size == 0:
                    continue

                edges = cv2.Canny(region, 80, 160)
                edge_density = float(
                    np.mean(edges > 0)
                )

                visual_variation = min(
                    float(np.std(region)) / 128,
                    1.0,
                )

                face_penalty = sum(
                    overlap_ratio(rectangle, face) * 8
                    for face in faces
                )

                score = (
                    edge_density * 3
                    + visual_variation * 0.35
                    + face_penalty
                )

                measurements[candidate["name"]][
                    "scores"
                ].append(score)

                measurements[candidate["name"]][
                    "brightness"
                ].append(float(np.median(region)))

        result = []

        for candidate in self.candidates:
            name = candidate["name"]
            data = measurements[name]

            average_score = (
                float(np.mean(data["scores"]))
                if data["scores"]
                else 999.0
            )

            brightness = (
                float(np.median(data["brightness"]))
                if data["brightness"]
                else 100.0
            )

            result.append({
                **candidate,
                "score": average_score,
                "brightness": brightness,
            })

        result.sort(key=lambda item: item["score"])
        selected = result[0]

        if self.previous_candidate is not None:
            previous = next(
                (
                    item
                    for item in result
                    if item["name"] == self.previous_candidate
                ),
                None,
            )

            if (
                previous is not None
                and previous["score"]
                <= selected["score"] * 1.15 + 0.03
            ):
                selected = previous

        self.previous_candidate = selected["name"]

        return selected, result

    def close(self):
        self.capture.release()


def choose_colors(brightness):
    if brightness >= 145:
        return {
            "text": "&H001C1C1C&",
            "outline": "&H00F2F2F2&",
        }

    return {
        "text": "&H00F5F5F5&",
        "outline": "&H00151515&",
    }


def create_ass(video_path, transcript_path, output_path):
    transcript = json.loads(
        transcript_path.read_text(encoding="utf-8-sig")
    )

    chunks = build_caption_chunks(transcript)

    if not chunks:
        raise RuntimeError(
            "Tidak ada teks yang dapat dijadikan subtitle."
        )

    analyzer = FrameAnalyzer(video_path)
    events = []
    analysis_log = []

    try:
        for chunk in chunks:
            selected, candidates = analyzer.analyze(
                chunk["start"],
                chunk["end"],
            )

            colors = choose_colors(
                selected["brightness"]
            )

            position_x = PLAY_RES_X // 2
            position_y = round(
                PLAY_RES_Y * selected["y_ratio"]
            )

            start_y = position_y + 10
            text = wrap_phrase(
                ass_escape(chunk["text"])
            )

            animation = (
                r"{"
                r"\an5"
                f"\\move({position_x},{start_y},"
                f"{position_x},{position_y},0,260)"
                r"\fad(220,300)"
                r"\fscx96\fscy96"
                r"\t(0,260,\fscx100\fscy100)"
                f"\\1c{colors['text']}"
                f"\\3c{colors['outline']}"
                r"\bord2.2"
                r"\shad0"
                r"\blur0.45"
                r"}"
            )

            events.append(
                "Dialogue: 0,"
                f"{ass_time(chunk['start'])},"
                f"{ass_time(chunk['end'])},"
                "Cinematic,,0,0,0,,"
                f"{animation}{text}"
            )

            analysis_log.append({
                "start": round(chunk["start"], 3),
                "end": round(chunk["end"], 3),
                "text": chunk["text"],
                "position": selected["name"],
                "brightness": round(
                    selected["brightness"],
                    2,
                ),
                "text_color": colors["text"],
                "candidate_scores": {
                    item["name"]: round(item["score"], 4)
                    for item in candidates
                },
            })
    finally:
        analyzer.close()

    ass_header = f"""[Script Info]
Title: Hermes Cinematic Dynamic Captions
ScriptType: v4.00+
PlayResX: {PLAY_RES_X}
PlayResY: {PLAY_RES_Y}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cinematic,Segoe UI Semibold,32,&H00F5F5F5,&H00F5F5F5,&H00151515,&H00000000,-1,0,0,0,100,100,0.3,0,1,2.2,0,2,60,60,80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    output_path.write_text(
        ass_header + "\n".join(events) + "\n",
        encoding="utf-8-sig",
    )

    analysis_path = (
        output_path.parent
        / "caption-analysis.json"
    )

    analysis_path.write_text(
        json.dumps(
            analysis_log,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("Subtitle cinematic berhasil dibuat.")
    print(f"Jumlah tampilan : {len(events)}")
    print(f"File ASS        : {output_path}")
    print(f"Analisis posisi : {analysis_path}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(
            "Pemakaian: make_cinematic_ass.py "
            "<video.mp4> <transcript.json> <output.ass>"
        )

    create_ass(
        Path(sys.argv[1]).resolve(),
        Path(sys.argv[2]).resolve(),
        Path(sys.argv[3]).resolve(),
    )
