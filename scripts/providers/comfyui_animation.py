#!/usr/bin/env python3
"""Generate a Hermes animation asset through local ComfyUI and Wan 2.2 TI2V."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib import error, parse, request


DEFAULT_NEGATIVE_PROMPT = (
    "overexposed, static image, blurry, low quality, distorted anatomy, extra limbs, "
    "extra fingers, malformed hands, malformed face, duplicate person, identity change, "
    "costume change, flicker, jitter, violent motion, sudden camera movement, scene cut, "
    "text, watermark, logo"
)
DEFAULT_PROMPT_SUFFIX = (
    "cinematic 2D animated film, stable character design, coherent anatomy, restrained "
    "colors, subtle natural motion, preserve the reference identity, no scene cut"
)


class ComfyUIAnimationError(RuntimeError):
    """A Wan animation request failed or returned an invalid result."""


def _compact(value: Any, limit: int = 1600) -> str:
    return " ".join(str(value).replace("\x00", "").split())[:limit]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_hash(value: Any, *, label: str = "SHA-256") -> str:
    normalized = str(value or "").strip().lower()
    if normalized.startswith("sha256:"):
        normalized = normalized.split(":", 1)[1]
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise ComfyUIAnimationError(f"{label} harus berupa SHA-256 64 digit hex.")
    return normalized


def _safe_name(value: Any, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-.")
    return (cleaned or fallback)[:80]


def _node_output(node: str, index: int) -> list[Any]:
    return [node, index]


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ComfyUIAnimationError(f"{label} tidak dapat dibaca: {_compact(exc)}") from None
    if not isinstance(value, dict):
        raise ComfyUIAnimationError(f"{label} harus berupa object JSON.")
    return value


def _resolved_child(base: Path, value: Any, label: str) -> Path:
    candidate = (base / str(value or "")).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        raise ComfyUIAnimationError(f"{label} keluar dari production root.") from None
    return candidate


@dataclass(frozen=True)
class GenerationSettings:
    diffusion_model: str = "wan2.2_ti2v_5B_fp16.safetensors"
    text_encoder: str = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
    vae: str = "wan2.2_vae.safetensors"
    steps: int = 20
    cfg: float = 5.0
    sampler: str = "uni_pc"
    scheduler: str = "simple"
    shift: float = 8.0
    weight_dtype: str = "default"
    negative_prompt: str = DEFAULT_NEGATIVE_PROMPT
    prompt_suffix: str = DEFAULT_PROMPT_SUFFIX
    filename_prefix: str = "HermesAI/provider-animation"
    generation_width: int | None = None
    generation_height: int | None = None
    max_frames: int = 49


def validate_request(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ComfyUIAnimationError("Hermes asset request harus berupa object JSON.")
    if payload.get("kind") != "animation":
        raise ComfyUIAnimationError("Bridge Wan ini hanya menerima asset kind 'animation'.")
    asset_request = payload.get("request")
    if not isinstance(asset_request, dict):
        raise ComfyUIAnimationError("Hermes asset request tidak memiliki object 'request'.")
    if not str(asset_request.get("prompt") or "").strip():
        raise ComfyUIAnimationError("Animation request tidak memiliki prompt.")
    if asset_request.get("reference_dependency") != "reference":
        raise ComfyUIAnimationError(
            "Animation request wajib memakai reference_dependency 'reference'."
        )
    for key in ("width", "height"):
        value = asset_request.get(key)
        if type(value) is not int or value < 64 or value > 4096:
            raise ComfyUIAnimationError(f"{key} harus integer 64-4096.")
    seed = asset_request.get("seed")
    if type(seed) is not int or not 0 <= seed <= 0xFFFFFFFFFFFFFFFF:
        raise ComfyUIAnimationError("seed harus integer unsigned 64-bit.")
    for key, maximum in (("fps", 120.0), ("duration_seconds", 120.0)):
        value = asset_request.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ComfyUIAnimationError(f"{key} harus berupa angka.")
        if not 0 < float(value) <= maximum:
            raise ComfyUIAnimationError(f"{key} harus > 0 dan <= {maximum:g}.")
    dependencies = payload.get("dependency_artifacts")
    if not isinstance(dependencies, list) or len(dependencies) != 1:
        raise ComfyUIAnimationError(
            "Animation harus memiliki tepat satu dependency artifact reference."
        )
    _normalized_hash(dependencies[0], label="Fingerprint dependency reference")
    return asset_request


def wan_frame_count(duration_seconds: float, fps: float, max_frames: int) -> int:
    """Return a Wan-compatible 4n+1 frame count, capped by local policy."""
    desired = max(1, round(float(duration_seconds) * float(fps)))
    normalized = ((desired - 1 + 3) // 4) * 4 + 1
    return min(normalized, max_frames)


def _generation_dimensions(
    asset_request: dict[str, Any], settings: GenerationSettings
) -> tuple[int, int]:
    if settings.generation_width is not None:
        return settings.generation_width, settings.generation_height or 0
    width = max(64, int(asset_request["width"]) // 32 * 32)
    height = max(64, int(asset_request["height"]) // 32 * 32)
    return width, height


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def build_workflow(
    payload: dict[str, Any],
    settings: GenerationSettings,
    *,
    reference_input: str,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    asset_request = validate_request(payload)
    generation_width, generation_height = _generation_dimensions(asset_request, settings)
    frames = wan_frame_count(
        float(asset_request["duration_seconds"]),
        float(asset_request["fps"]),
        settings.max_frames,
    )
    prompt_parts = [str(asset_request["prompt"]).strip()]
    for label, key in (
        ("camera", "camera"),
        ("continuity", "continuity_notes"),
        ("characters", "characters"),
        ("location", "location"),
    ):
        value = _text(asset_request.get(key))
        if value:
            prompt_parts.append(f"{label}: {value}")
    if settings.prompt_suffix.strip():
        prompt_parts.append(settings.prompt_suffix.strip())
    prompt = "; ".join(prompt_parts)
    shot_id = _safe_name(payload.get("shot_id"), "shot")
    fingerprint = str(payload.get("input_fingerprint") or "")
    suffix = re.sub(r"[^0-9a-f]", "", fingerprint.lower())[-12:] or "untracked"
    filename_prefix = f"{settings.filename_prefix.rstrip('/')}/{shot_id}-{suffix}"

    workflow: dict[str, Any] = {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {
                "unet_name": settings.diffusion_model,
                "weight_dtype": settings.weight_dtype,
            },
        },
        "2": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": settings.text_encoder,
                "type": "wan",
                "device": "default",
            },
        },
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": settings.vae}},
        "4": {"class_type": "LoadImage", "inputs": {"image": reference_input}},
        "5": {
            "class_type": "ModelSamplingSD3",
            "inputs": {"model": _node_output("1", 0), "shift": settings.shift},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": _node_output("2", 0)},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": settings.negative_prompt,
                "clip": _node_output("2", 0),
            },
        },
        "8": {
            "class_type": "Wan22ImageToVideoLatent",
            "inputs": {
                "vae": _node_output("3", 0),
                "start_image": _node_output("4", 0),
                "width": generation_width,
                "height": generation_height,
                "length": frames,
                "batch_size": 1,
            },
        },
        "9": {
            "class_type": "KSampler",
            "inputs": {
                "model": _node_output("5", 0),
                "positive": _node_output("6", 0),
                "negative": _node_output("7", 0),
                "latent_image": _node_output("8", 0),
                "seed": asset_request["seed"],
                "steps": settings.steps,
                "cfg": settings.cfg,
                "sampler_name": settings.sampler,
                "scheduler": settings.scheduler,
                "denoise": 1.0,
            },
        },
        "10": {
            "class_type": "VAEDecode",
            "inputs": {"samples": _node_output("9", 0), "vae": _node_output("3", 0)},
        },
        "11": {
            "class_type": "ImageScale",
            "inputs": {
                "image": _node_output("10", 0),
                "upscale_method": "lanczos",
                "width": asset_request["width"],
                "height": asset_request["height"],
                "crop": "center",
            },
        },
        "12": {
            "class_type": "CreateVideo",
            "inputs": {"images": _node_output("11", 0), "fps": asset_request["fps"]},
        },
        "13": {
            "class_type": "SaveVideo",
            "inputs": {
                "video": _node_output("12", 0),
                "filename_prefix": filename_prefix,
                "format": "mp4",
                "codec": "h264",
            },
        },
    }
    metadata = {
        "requested_width": asset_request["width"],
        "requested_height": asset_request["height"],
        "generation_width": generation_width,
        "generation_height": generation_height,
        "requested_frames": round(
            float(asset_request["duration_seconds"]) * float(asset_request["fps"])
        ),
        "generation_frames": frames,
        "duration_clamped": frames < round(
            float(asset_request["duration_seconds"]) * float(asset_request["fps"])
        ),
    }
    return workflow, "13", metadata


def resolve_reference(
    request_path: Path,
    payload: dict[str, Any],
    *,
    reference_image: Path | None = None,
    reference_sha256: str | None = None,
) -> tuple[Path, str]:
    validate_request(payload)
    expected = _normalized_hash(
        payload["dependency_artifacts"][0], label="Fingerprint dependency reference"
    )
    if reference_image is not None:
        path = reference_image.expanduser().resolve()
        if not path.is_file():
            raise ComfyUIAnimationError(f"Gambar reference tidak ditemukan: {path}")
        if reference_sha256 and _normalized_hash(reference_sha256) != expected:
            raise ComfyUIAnimationError(
                "--reference-sha256 tidak cocok dengan dependency artifact request."
            )
        actual = _sha256(path)
        if actual != expected:
            raise ComfyUIAnimationError(
                f"SHA-256 reference tidak sesuai: expected={expected}, actual={actual}"
            )
        return path, actual
    if reference_sha256:
        raise ComfyUIAnimationError("--reference-sha256 memerlukan --reference-image.")

    request_path = request_path.expanduser().resolve()
    shot_id = _safe_name(payload.get("shot_id"), "shot")
    production_root = None
    reference_root = None
    for parent in request_path.parents:
        candidate = parent / "attempts" / shot_id / "reference"
        if candidate.is_dir():
            production_root = parent
            reference_root = candidate
            break
    if production_root is None or reference_root is None:
        raise ComfyUIAnimationError(
            "Production root/reference attempt tidak ditemukan dari lokasi request animation."
        )

    matches: list[Path] = []
    for attempt_path in sorted(reference_root.glob("*/attempt.json")):
        try:
            attempt = _read_json(attempt_path, "Reference attempt")
            output = attempt.get("output")
            if (
                attempt.get("shot_id") != payload.get("shot_id")
                or attempt.get("kind") != "reference"
                or attempt.get("state") != "COMPLETED"
                or not isinstance(output, dict)
                or _normalized_hash(output.get("fingerprint")) != expected
            ):
                continue
            output_path = _resolved_child(production_root, output.get("path"), "Reference output")
            if output_path.is_file() and _sha256(output_path) == expected:
                matches.append(output_path)
        except ComfyUIAnimationError:
            continue
    if not matches:
        raise ComfyUIAnimationError(
            "Tidak ada completed reference attempt yang cocok dengan dependency fingerprint."
        )
    return matches[-1], expected


class ComfyUIClient:
    def __init__(self, server: str, *, timeout: float = 30.0):
        normalized = server.strip().rstrip("/")
        parsed = parse.urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ComfyUIAnimationError("URL ComfyUI tidak valid.")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ComfyUIAnimationError("Bridge hanya mengizinkan server ComfyUI loopback lokal.")
        self.server = normalized
        self.timeout = timeout

    def _open(self, req: request.Request, *, timeout: float | None = None) -> bytes:
        try:
            with request.urlopen(req, timeout=timeout or self.timeout) as response:
                return response.read()
        except error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace")
            raise ComfyUIAnimationError(
                f"ComfyUI HTTP {exc.code}: {_compact(detail) or exc.reason}"
            ) from None
        except (error.URLError, TimeoutError, OSError) as exc:
            raise ComfyUIAnimationError(
                f"ComfyUI tidak dapat dihubungi: {_compact(exc)}"
            ) from None

    def json(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None
        headers: dict[str, str] = {}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(self.server + path, data=body, headers=headers, method=method)
        raw = self._open(req)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ComfyUIAnimationError(
                f"Respons JSON ComfyUI tidak valid: {_compact(exc)}"
            ) from None
        if not isinstance(value, dict):
            raise ComfyUIAnimationError("Respons JSON ComfyUI bukan object.")
        return value

    def upload_image(self, source: Path, *, content_hash: str) -> str:
        boundary = "----HermesComfyUI" + uuid.uuid4().hex
        subfolder = "HermesAI/provider-inputs"
        filename = f"animation-reference-{content_hash[:16]}{source.suffix.lower() or '.png'}"
        mime_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        chunks: list[bytes] = []
        for name, value in (("overwrite", "true"), ("subfolder", subfolder), ("type", "input")):
            chunks.append(
                (
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\""
                    f"\r\n\r\n{value}\r\n"
                ).encode("utf-8")
            )
        chunks.append(
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
                f"filename=\"{filename}\"\r\nContent-Type: {mime_type}\r\n\r\n"
            ).encode("utf-8")
            + source.read_bytes()
            + b"\r\n"
        )
        chunks.append(f"--{boundary}--\r\n".encode("ascii"))
        req = request.Request(
            self.server + "/upload/image",
            data=b"".join(chunks),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        raw = self._open(req, timeout=max(self.timeout, 120.0))
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ComfyUIAnimationError(
                f"Respons upload ComfyUI tidak valid: {_compact(exc)}"
            ) from None
        name = str(result.get("name") or "").strip() if isinstance(result, dict) else ""
        uploaded_subfolder = (
            str(result.get("subfolder") or "").strip() if isinstance(result, dict) else ""
        )
        if not name:
            raise ComfyUIAnimationError("ComfyUI tidak mengembalikan nama image upload.")
        return str(PurePosixPath(uploaded_subfolder) / name) if uploaded_subfolder else name

    def queue_prompt(self, workflow: dict[str, Any], *, client_id: str) -> str:
        result = self.json(
            "/prompt", method="POST", payload={"client_id": client_id, "prompt": workflow}
        )
        prompt_id = str(result.get("prompt_id") or "").strip()
        node_errors = result.get("node_errors")
        if not prompt_id or (isinstance(node_errors, dict) and node_errors):
            raise ComfyUIAnimationError(
                "Workflow ditolak ComfyUI: " + _compact(node_errors or result)
            )
        return prompt_id

    @staticmethod
    def _file_info(value: Any) -> dict[str, str] | None:
        if isinstance(value, dict):
            filename = str(value.get("filename") or "").strip()
            if filename:
                return {
                    "filename": filename,
                    "subfolder": str(value.get("subfolder") or ""),
                    "type": str(value.get("type") or "output"),
                }
            for nested in value.values():
                match = ComfyUIClient._file_info(nested)
                if match:
                    return match
        elif isinstance(value, list):
            for nested in value:
                match = ComfyUIClient._file_info(nested)
                if match:
                    return match
        return None

    def wait_for_video(
        self,
        prompt_id: str,
        *,
        output_node: str,
        timeout: float,
        poll_interval: float,
    ) -> dict[str, str]:
        deadline = time.monotonic() + timeout
        encoded_id = parse.quote(prompt_id, safe="")
        while time.monotonic() < deadline:
            history = self.json(f"/history/{encoded_id}")
            entry = history.get(prompt_id)
            if isinstance(entry, dict):
                status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
                if status.get("status_str") != "success" or not status.get("completed"):
                    raise ComfyUIAnimationError(
                        "Eksekusi ComfyUI gagal: " + _compact(status.get("messages") or status)
                    )
                outputs = entry.get("outputs") if isinstance(entry.get("outputs"), dict) else {}
                node = outputs.get(output_node)
                info = self._file_info(node)
                if not info:
                    raise ComfyUIAnimationError("Workflow ComfyUI selesai tanpa output video.")
                return info
            time.sleep(poll_interval)
        raise ComfyUIAnimationError(f"Timeout menunggu ComfyUI prompt {prompt_id}.")

    def download_output(self, info: dict[str, str]) -> bytes:
        query = parse.urlencode(
            {"filename": info["filename"], "subfolder": info["subfolder"], "type": info["type"]}
        )
        req = request.Request(self.server + "/view?" + query, method="GET")
        return self._open(req, timeout=max(self.timeout, 300.0))

    @staticmethod
    def _model_choices(info: dict[str, Any], node: str, input_name: str) -> list[str]:
        try:
            values = info[node]["input"]["required"][input_name][0]
        except (KeyError, IndexError, TypeError):
            raise ComfyUIAnimationError(
                f"Metadata {node}.{input_name} tidak dapat dibaca."
            ) from None
        return values if isinstance(values, list) else []

    def healthcheck(self, settings: GenerationSettings) -> dict[str, Any]:
        stats = self.json("/system_stats")
        info = self.json("/object_info")
        required_nodes = {
            "UNETLoader",
            "CLIPLoader",
            "VAELoader",
            "LoadImage",
            "ModelSamplingSD3",
            "Wan22ImageToVideoLatent",
            "KSampler",
            "VAEDecode",
            "ImageScale",
            "CreateVideo",
            "SaveVideo",
        }
        missing = sorted(node for node in required_nodes if not info.get(node))
        if missing:
            raise ComfyUIAnimationError("Node ComfyUI tidak tersedia: " + ", ".join(missing))
        checks = (
            ("UNETLoader", "unet_name", settings.diffusion_model),
            ("CLIPLoader", "clip_name", settings.text_encoder),
            ("VAELoader", "vae_name", settings.vae),
        )
        for node, input_name, model in checks:
            if model not in self._model_choices(info, node, input_name):
                raise ComfyUIAnimationError(f"Model tidak ditemukan oleh {node}: {model}")
        return {
            "ready": True,
            "server": self.server,
            "devices": stats.get("devices", []),
            "diffusion_model": settings.diffusion_model,
            "text_encoder": settings.text_encoder,
            "vae": settings.vae,
            "max_frames": settings.max_frames,
        }


def generate_animation(
    request_path: Path,
    output_path: Path,
    *,
    client: ComfyUIClient,
    settings: GenerationSettings,
    execution_timeout: float,
    poll_interval: float,
    reference_image: Path | None = None,
    reference_sha256: str | None = None,
) -> dict[str, Any]:
    payload = _read_json(request_path, "Asset request")
    validate_request(payload)
    reference_path, reference_hash = resolve_reference(
        request_path,
        payload,
        reference_image=reference_image,
        reference_sha256=reference_sha256,
    )
    reference_input = client.upload_image(reference_path, content_hash=reference_hash)
    workflow, output_node, metadata = build_workflow(
        payload, settings, reference_input=reference_input
    )
    prompt_id = client.queue_prompt(workflow, client_id="hermes-" + uuid.uuid4().hex)
    video_info = client.wait_for_video(
        prompt_id,
        output_node=output_node,
        timeout=execution_timeout,
        poll_interval=poll_interval,
    )
    video_bytes = client.download_output(video_info)
    if len(video_bytes) < 12 or video_bytes[4:8] != b"ftyp":
        raise ComfyUIAnimationError("Output ComfyUI bukan file MP4 yang valid.")
    _atomic_bytes(output_path, video_bytes)
    return {
        "prompt_id": prompt_id,
        "output": str(output_path),
        "size_bytes": len(video_bytes),
        "reference_sha256": reference_hash,
        **metadata,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes Wan 2.2 animation bridge for ComfyUI.")
    parser.add_argument("--request")
    parser.add_argument("--output")
    parser.add_argument(
        "--server", default=os.environ.get("HERMES_COMFYUI_URL", "http://127.0.0.1:8188")
    )
    parser.add_argument("--diffusion-model", default="wan2.2_ti2v_5B_fp16.safetensors")
    parser.add_argument(
        "--text-encoder", default="umt5_xxl_fp8_e4m3fn_scaled.safetensors"
    )
    parser.add_argument("--vae", default="wan2.2_vae.safetensors")
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--cfg", type=float, default=5.0)
    parser.add_argument("--sampler", default="uni_pc")
    parser.add_argument("--scheduler", default="simple")
    parser.add_argument("--shift", type=float, default=8.0)
    parser.add_argument("--weight-dtype", default="default")
    parser.add_argument("--negative-prompt", default=DEFAULT_NEGATIVE_PROMPT)
    parser.add_argument("--prompt-suffix", default=DEFAULT_PROMPT_SUFFIX)
    parser.add_argument("--filename-prefix", default="HermesAI/provider-animation")
    parser.add_argument("--generation-width", type=int)
    parser.add_argument("--generation-height", type=int)
    parser.add_argument("--max-frames", type=int, default=49)
    parser.add_argument("--reference-image")
    parser.add_argument("--reference-sha256")
    parser.add_argument("--execution-timeout", type=float, default=3600.0)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--health-check", action="store_true")
    return parser


def _settings(args: argparse.Namespace) -> GenerationSettings:
    if not 1 <= args.steps <= 200:
        raise ComfyUIAnimationError("--steps harus berada pada rentang 1-200.")
    if not 0 < args.cfg <= 100:
        raise ComfyUIAnimationError("--cfg harus > 0 dan <= 100.")
    if not 0 <= args.shift <= 100:
        raise ComfyUIAnimationError("--shift harus berada pada rentang 0-100.")
    if not 1 <= args.max_frames <= 401 or (args.max_frames - 1) % 4:
        raise ComfyUIAnimationError("--max-frames harus berada pada pola Wan 4n+1.")
    dimensions = (args.generation_width, args.generation_height)
    if (dimensions[0] is None) != (dimensions[1] is None):
        raise ComfyUIAnimationError(
            "--generation-width dan --generation-height harus dipakai berpasangan."
        )
    for name, value in zip(("width", "height"), dimensions):
        if value is not None and (value < 64 or value > 4096 or value % 32):
            raise ComfyUIAnimationError(
                f"--generation-{name} harus 64-4096 dan kelipatan 32."
            )
    if args.execution_timeout <= 0 or args.poll_interval <= 0:
        raise ComfyUIAnimationError("Timeout dan poll interval harus lebih besar dari nol.")
    return GenerationSettings(
        diffusion_model=args.diffusion_model,
        text_encoder=args.text_encoder,
        vae=args.vae,
        steps=args.steps,
        cfg=args.cfg,
        sampler=args.sampler,
        scheduler=args.scheduler,
        shift=args.shift,
        weight_dtype=args.weight_dtype,
        negative_prompt=args.negative_prompt,
        prompt_suffix=args.prompt_suffix,
        filename_prefix=args.filename_prefix,
        generation_width=args.generation_width,
        generation_height=args.generation_height,
        max_frames=args.max_frames,
    )


def main() -> int:
    args = build_parser().parse_args()
    try:
        settings = _settings(args)
        client = ComfyUIClient(args.server)
        if args.health_check:
            result = client.healthcheck(settings)
        else:
            if not args.request or not args.output:
                raise ComfyUIAnimationError("--request dan --output wajib untuk generation.")
            result = generate_animation(
                Path(args.request).expanduser().resolve(),
                Path(args.output).expanduser().resolve(),
                client=client,
                settings=settings,
                execution_timeout=args.execution_timeout,
                poll_interval=args.poll_interval,
                reference_image=(
                    Path(args.reference_image).expanduser().resolve()
                    if args.reference_image
                    else None
                ),
                reference_sha256=args.reference_sha256,
            )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ComfyUIAnimationError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nProvider Wan dihentikan oleh operator.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
