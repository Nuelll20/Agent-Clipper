import json
import subprocess
from pathlib import Path

import pytest

from scripts.providers import video_finalize


def _completed(
    command,
    *,
    stdout="",
    stderr="",
    returncode=0,
):
    return subprocess.CompletedProcess(
        command,
        returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _probe(
    *,
    width=720,
    height=1280,
    codec="h264",
    fps=60.0,
    duration=2.0,
    has_audio=False,
):
    return video_finalize.VideoProbe(
        width=width,
        height=height,
        codec_name=codec,
        frame_rate=fps,
        duration=duration,
        nb_frames=round(fps * duration),
        size_bytes=100,
        has_audio=has_audio,
        audio_codec="aac" if has_audio else None,
        audio_sample_rate=48000 if has_audio else None,
        audio_channels=2 if has_audio else None,
    )


def _source_file(tmp_path: Path) -> Path:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-video")
    return source


def test_healthcheck_detects_minterpolate_and_libx264():
    def runner(command, **kwargs):
        if "-filters" in command:
            return _completed(command, stdout="... minterpolate ...")
        if "-encoders" in command:
            return _completed(command, stdout="... libx264 ...")
        return _completed(command, stdout="ffprobe version test")

    result = video_finalize.healthcheck(runner=runner)

    assert result["ready"] is True
    assert result["interpolation_method"] == "ffmpeg-minterpolate"
    assert result["video_encoder"] == "libx264"


def test_missing_source_rejected(tmp_path):
    with pytest.raises(
        video_finalize.VideoFinalizeError,
        match="does not exist or is empty",
    ):
        video_finalize.finalize_video(
            tmp_path / "missing.mp4",
            tmp_path / "final.mp4",
        )


def test_same_input_and_output_rejected(tmp_path):
    source = _source_file(tmp_path)

    with pytest.raises(
        video_finalize.VideoFinalizeError,
        match="must be different",
    ):
        video_finalize.finalize_video(source, source)


def test_existing_output_rejected(tmp_path):
    source = _source_file(tmp_path)
    output = tmp_path / "final.mp4"
    output.write_bytes(b"existing")

    with pytest.raises(
        video_finalize.VideoFinalizeError,
        match="already exists",
    ):
        video_finalize.finalize_video(source, output)


def test_probe_parses_rational_frame_rate(tmp_path):
    source = _source_file(tmp_path)
    payload = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 720,
                "height": 1280,
                "r_frame_rate": "60000/1001",
                "avg_frame_rate": "60000/1001",
                "nb_frames": "120",
            }
        ],
        "format": {"duration": "2.002"},
    }

    def runner(command, **kwargs):
        return _completed(command, stdout=json.dumps(payload))

    probe = video_finalize.probe_video(source, runner=runner)

    assert probe.frame_rate == pytest.approx(59.94005994)
    assert probe.duration == pytest.approx(2.002)
    assert probe.nb_frames == 120


def test_probe_rejects_file_without_video_stream(tmp_path):
    source = _source_file(tmp_path)
    payload = {
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
            }
        ],
        "format": {"duration": "2.0"},
    }

    def runner(command, **kwargs):
        return _completed(command, stdout=json.dumps(payload))

    with pytest.raises(
        video_finalize.VideoFinalizeError,
        match="no video stream",
    ):
        video_finalize.probe_video(source, runner=runner)


def test_ffmpeg_failure_cleans_temporary_file(tmp_path, monkeypatch):
    source = _source_file(tmp_path)
    output = tmp_path / "final.mp4"
    monkeypatch.setattr(
        video_finalize,
        "probe_video",
        lambda *args, **kwargs: _probe(fps=24.0),
    )

    def runner(command, **kwargs):
        Path(command[-1]).write_bytes(b"partial")
        return _completed(command, returncode=1, stderr="encode failed")

    with pytest.raises(
        video_finalize.VideoFinalizeError,
        match="encode failed",
    ):
        video_finalize.finalize_video(
            source,
            output,
            runner=runner,
        )

    assert not output.exists()
    assert not list(tmp_path.glob("*.tmp.mp4"))


@pytest.mark.parametrize(
    ("final_probe", "message"),
    [
        (_probe(width=1080), "resolution is invalid"),
        (_probe(fps=30.0), "frame rate"),
        (_probe(codec="hevc"), "codec"),
        (_probe(duration=2.25), "duration changed"),
    ],
)
def test_invalid_final_probe_rejected(
    tmp_path,
    monkeypatch,
    final_probe,
    message,
):
    source = _source_file(tmp_path)
    output = tmp_path / "final.mp4"
    probes = iter([_probe(fps=24.0), final_probe])
    monkeypatch.setattr(
        video_finalize,
        "probe_video",
        lambda *args, **kwargs: next(probes),
    )

    def runner(command, **kwargs):
        Path(command[-1]).write_bytes(b"rendered")
        return _completed(command)

    with pytest.raises(
        video_finalize.VideoFinalizeError,
        match=message,
    ):
        video_finalize.finalize_video(
            source,
            output,
            runner=runner,
        )

    assert not output.exists()
    assert not list(tmp_path.glob("*.tmp.mp4"))


def test_source_audio_must_be_preserved(tmp_path, monkeypatch):
    source = _source_file(tmp_path)
    output = tmp_path / "final.mp4"
    probes = iter([
        _probe(fps=24.0, has_audio=True),
        _probe(has_audio=False),
    ])
    monkeypatch.setattr(
        video_finalize,
        "probe_video",
        lambda *args, **kwargs: next(probes),
    )

    def runner(command, **kwargs):
        Path(command[-1]).write_bytes(b"rendered")
        return _completed(command)

    with pytest.raises(
        video_finalize.VideoFinalizeError,
        match="lost the source audio",
    ):
        video_finalize.finalize_video(
            source,
            output,
            runner=runner,
        )


def test_success_returns_source_and_final_metadata(tmp_path, monkeypatch):
    source = _source_file(tmp_path)
    output = tmp_path / "final.mp4"
    probes = iter([
        _probe(fps=24.0, duration=2.04),
        _probe(duration=2.00),
    ])
    monkeypatch.setattr(
        video_finalize,
        "probe_video",
        lambda *args, **kwargs: next(probes),
    )

    def runner(command, **kwargs):
        Path(command[-1]).write_bytes(b"final-video")
        return _completed(command)

    result = video_finalize.finalize_video(
        source,
        output,
        runner=runner,
    )

    assert output.read_bytes() == b"final-video"
    assert result["output"] == str(output.resolve())
    assert result["interpolation_method"] == "ffmpeg-minterpolate"
    assert result["source_probe"]["frame_rate"] == 24.0
    assert result["final_probe"]["frame_rate"] == 60.0
    assert result["duration_delta"] == pytest.approx(0.04)
