#!/usr/bin/env python3
"""Generate a Hermes reference image through a local ComfyUI HTTP API."""

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
    "text, watermark, logo, duplicate character, duplicate person, multiple people, "
    "extra limbs, extra fingers, deformed hands, malformed face, distorted body, blurry, "
    "low quality, oversaturated colors, plastic skin"
)
DEFAULT_PROMPT_SUFFIX = (
    "cinematic 2D animated film still, restrained color palette, clean composition, "
    "soft volumetric lighting, detailed environment, consistent facial features"
)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ComfyUIProviderError(RuntimeError):
    """A local ComfyUI request failed or returned an invalid result."""


def _compact(value: Any, limit: int = 1200) -> str:
    return " ".join(str(value).replace("\x00", "").split())[:limit]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_hash(value: str) -> str:
    normalized = value.strip().lower()
    if normalized.startswith("sha256:"):
        normalized = normalized.split(":", 1)[1]
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise ComfyUIProviderError("--reference-sha256 harus berupa SHA-256 64 digit hex.")
    return normalized


def _safe_name(value: Any, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-.")
    return (cleaned or fallback)[:80]


def _node_output(node: str, index: int) -> list[Any]:
    return [node, index]


@dataclass(frozen=True)
class GenerationSettings:
    checkpoint: str
    steps: int = 20
    cfg: float = 6.0
    sampler: str = "dpmpp_2m"
    scheduler: str = "karras"
    negative_prompt: str = DEFAULT_NEGATIVE_PROMPT
    prompt_suffix: str = DEFAULT_PROMPT_SUFFIX
    filename_prefix: str = "HermesAI/provider-reference"
    clip_vision_model: str = "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors"
    ipadapter_model: str = "ip-adapter-plus_sdxl_vit-h.safetensors"
    ipadapter_weight: float = 0.8
    ipadapter_start: float = 0.0
    ipadapter_end: float = 0.85


def validate_request(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ComfyUIProviderError("Hermes asset request harus berupa object JSON.")
    if payload.get("kind") != "reference":
        raise ComfyUIProviderError("Bridge ComfyUI ini hanya menerima asset kind 'reference'.")
    asset_request = payload.get("request")
    if not isinstance(asset_request, dict):
        raise ComfyUIProviderError("Hermes asset request tidak memiliki object 'request'.")
    prompt = str(asset_request.get("prompt") or "").strip()
    if not prompt:
        raise ComfyUIProviderError("Reference request tidak memiliki prompt.")
    for key in ("width", "height"):
        value = asset_request.get(key)
        if type(value) is not int or value < 64 or value > 4096 or value % 8:
            raise ComfyUIProviderError(f"{key} harus integer 64-4096 dan kelipatan 8.")
    seed = asset_request.get("seed")
    if type(seed) is not int or not 0 <= seed <= 0xFFFFFFFFFFFFFFFF:
        raise ComfyUIProviderError("seed harus integer unsigned 64-bit.")
    return asset_request


def build_workflow(
    payload: dict[str, Any],
    settings: GenerationSettings,
    *,
    reference_input: str | None = None,
) -> tuple[dict[str, Any], str]:
    asset_request = validate_request(payload)
    prompt = str(asset_request["prompt"]).strip()
    if settings.prompt_suffix.strip():
        prompt = f"{prompt}, {settings.prompt_suffix.strip()}"
    shot_id = _safe_name(payload.get("shot_id"), "shot")
    fingerprint = str(payload.get("input_fingerprint") or "")
    suffix = re.sub(r"[^0-9a-f]", "", fingerprint.lower())[-12:] or "untracked"
    filename_prefix = f"{settings.filename_prefix.rstrip('/')}/{shot_id}-{suffix}"

    workflow: dict[str, Any] = {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": settings.checkpoint},
        }
    }
    if reference_input:
        workflow.update({
            "2": {"class_type": "LoadImage", "inputs": {"image": reference_input}},
            "3": {
                "class_type": "IPAdapterModelLoader",
                "inputs": {"ipadapter_file": settings.ipadapter_model},
            },
            "4": {
                "class_type": "CLIPVisionLoader",
                "inputs": {"clip_name": settings.clip_vision_model},
            },
            "5": {
                "class_type": "IPAdapterAdvanced",
                "inputs": {
                    "model": _node_output("1", 0),
                    "ipadapter": _node_output("3", 0),
                    "image": _node_output("2", 0),
                    "clip_vision": _node_output("4", 0),
                    "weight": settings.ipadapter_weight,
                    "weight_type": "linear",
                    "combine_embeds": "concat",
                    "start_at": settings.ipadapter_start,
                    "end_at": settings.ipadapter_end,
                    "embeds_scaling": "V only",
                },
            },
        })
        model_source = _node_output("5", 0)
        node_ids = ("6", "7", "8", "9", "10", "11")
    else:
        model_source = _node_output("1", 0)
        node_ids = ("2", "3", "4", "5", "6", "7")

    positive_id, negative_id, latent_id, sampler_id, decode_id, save_id = node_ids
    workflow.update({
        positive_id: {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": _node_output("1", 1)},
        },
        negative_id: {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": settings.negative_prompt,
                "clip": _node_output("1", 1),
            },
        },
        latent_id: {
            "class_type": "EmptyLatentImage",
            "inputs": {
                "width": asset_request["width"],
                "height": asset_request["height"],
                "batch_size": 1,
            },
        },
        sampler_id: {
            "class_type": "KSampler",
            "inputs": {
                "seed": asset_request["seed"],
                "steps": settings.steps,
                "cfg": settings.cfg,
                "sampler_name": settings.sampler,
                "scheduler": settings.scheduler,
                "denoise": 1.0,
                "model": model_source,
                "positive": _node_output(positive_id, 0),
                "negative": _node_output(negative_id, 0),
                "latent_image": _node_output(latent_id, 0),
            },
        },
        decode_id: {
            "class_type": "VAEDecode",
            "inputs": {
                "samples": _node_output(sampler_id, 0),
                "vae": _node_output("1", 2),
            },
        },
        save_id: {
            "class_type": "SaveImage",
            "inputs": {
                "filename_prefix": filename_prefix,
                "images": _node_output(decode_id, 0),
            },
        },
    })
    return workflow, save_id


class ComfyUIClient:
    def __init__(self, server: str, *, timeout: float = 30.0):
        normalized = server.strip().rstrip("/")
        parsed = parse.urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ComfyUIProviderError("URL ComfyUI tidak valid.")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ComfyUIProviderError("Bridge hanya mengizinkan server ComfyUI loopback lokal.")
        self.server = normalized
        self.timeout = timeout

    def _open(self, req: request.Request, *, timeout: float | None = None) -> bytes:
        try:
            with request.urlopen(req, timeout=timeout or self.timeout) as response:
                return response.read()
        except error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace")
            raise ComfyUIProviderError(
                f"ComfyUI HTTP {exc.code}: {_compact(detail) or exc.reason}"
            ) from None
        except (error.URLError, TimeoutError, OSError) as exc:
            raise ComfyUIProviderError(f"ComfyUI tidak dapat dihubungi: {_compact(exc)}") from None

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
            raise ComfyUIProviderError(
                f"Respons JSON ComfyUI tidak valid: {_compact(exc)}"
            ) from None
        if not isinstance(value, dict):
            raise ComfyUIProviderError("Respons JSON ComfyUI bukan object.")
        return value

    def upload_image(self, source: Path, *, content_hash: str) -> str:
        boundary = "----HermesComfyUI" + uuid.uuid4().hex
        subfolder = "HermesAI/provider-inputs"
        filename = f"reference-{content_hash[:16]}{source.suffix.lower() or '.png'}"
        mime_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        chunks = []
        for name, value in (("overwrite", "true"), ("subfolder", subfolder), ("type", "input")):
            chunks.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n".encode("utf-8")
            )
        chunks.append(
            (
                f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"image\"; filename=\"{filename}\"\r\n"
                f"Content-Type: {mime_type}\r\n\r\n"
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
            raise ComfyUIProviderError(
                f"Respons upload ComfyUI tidak valid: {_compact(exc)}"
            ) from None
        name = str(result.get("name") or "").strip() if isinstance(result, dict) else ""
        uploaded_subfolder = (
            str(result.get("subfolder") or "").strip() if isinstance(result, dict) else ""
        )
        if not name:
            raise ComfyUIProviderError("ComfyUI tidak mengembalikan nama image upload.")
        return str(PurePosixPath(uploaded_subfolder) / name) if uploaded_subfolder else name

    def queue_prompt(self, workflow: dict[str, Any], *, client_id: str) -> str:
        result = self.json(
            "/prompt",
            method="POST",
            payload={"client_id": client_id, "prompt": workflow},
        )
        prompt_id = str(result.get("prompt_id") or "").strip()
        node_errors = result.get("node_errors")
        if not prompt_id or (isinstance(node_errors, dict) and node_errors):
            raise ComfyUIProviderError(
                "Workflow ditolak ComfyUI: " + _compact(node_errors or result)
            )
        return prompt_id

    def wait_for_image(
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
                    raise ComfyUIProviderError(
                        "Eksekusi ComfyUI gagal: " + _compact(status.get("messages") or status)
                    )
                outputs = entry.get("outputs") if isinstance(entry.get("outputs"), dict) else {}
                node = (
                    outputs.get(output_node)
                    if isinstance(outputs.get(output_node), dict)
                    else {}
                )
                images = node.get("images") if isinstance(node, dict) else None
                if not isinstance(images, list) or not images or not isinstance(images[0], dict):
                    raise ComfyUIProviderError("Workflow ComfyUI selesai tanpa output gambar.")
                image = images[0]
                filename = str(image.get("filename") or "").strip()
                if not filename:
                    raise ComfyUIProviderError("Output ComfyUI tidak memiliki filename.")
                return {
                    "filename": filename,
                    "subfolder": str(image.get("subfolder") or ""),
                    "type": str(image.get("type") or "output"),
                }
            time.sleep(poll_interval)
        raise ComfyUIProviderError(f"Timeout menunggu ComfyUI prompt {prompt_id}.")

    def download_image(self, image: dict[str, str]) -> bytes:
        query = parse.urlencode({
            "filename": image["filename"],
            "subfolder": image["subfolder"],
            "type": image["type"],
        })
        req = request.Request(self.server + "/view?" + query, method="GET")
        return self._open(req, timeout=max(self.timeout, 120.0))

    def healthcheck(
        self,
        settings: GenerationSettings,
        *,
        require_ipadapter: bool,
    ) -> dict[str, Any]:
        stats = self.json("/system_stats")
        checkpoint_info = self.json("/object_info/CheckpointLoaderSimple")
        try:
            checkpoints = checkpoint_info["CheckpointLoaderSimple"]["input"]["required"][
                "ckpt_name"
            ][0]
        except (KeyError, IndexError, TypeError):
            raise ComfyUIProviderError(
                "Metadata CheckpointLoaderSimple tidak dapat dibaca."
            ) from None
        if settings.checkpoint not in checkpoints:
            raise ComfyUIProviderError(
                f"Checkpoint tidak ditemukan oleh ComfyUI: {settings.checkpoint}"
            )
        result = {
            "ready": True,
            "server": self.server,
            "checkpoint": settings.checkpoint,
            "devices": stats.get("devices", []),
            "ipadapter": False,
        }
        if require_ipadapter:
            clip_info = self.json("/object_info/CLIPVisionLoader")
            adapter_info = self.json("/object_info/IPAdapterModelLoader")
            advanced_info = self.json("/object_info/IPAdapterAdvanced")
            try:
                clips = clip_info["CLIPVisionLoader"]["input"]["required"]["clip_name"][0]
                adapters = adapter_info["IPAdapterModelLoader"]["input"]["required"][
                    "ipadapter_file"
                ][0]
            except (KeyError, IndexError, TypeError):
                raise ComfyUIProviderError(
                    "Metadata model IP-Adapter tidak dapat dibaca."
                ) from None
            if not advanced_info.get("IPAdapterAdvanced"):
                raise ComfyUIProviderError("Node IPAdapterAdvanced tidak tersedia.")
            if settings.clip_vision_model not in clips:
                raise ComfyUIProviderError(
                    f"CLIP Vision tidak ditemukan oleh ComfyUI: {settings.clip_vision_model}"
                )
            if settings.ipadapter_model not in adapters:
                raise ComfyUIProviderError(
                    f"Model IP-Adapter tidak ditemukan oleh ComfyUI: {settings.ipadapter_model}"
                )
            result["ipadapter"] = True
        return result


def generate_reference(
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
    try:
        payload = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ComfyUIProviderError(f"Asset request tidak dapat dibaca: {_compact(exc)}") from None

    reference_input = None
    reference_hash = None
    if reference_image is not None:
        if not reference_image.is_file():
            raise ComfyUIProviderError(f"Gambar referensi tidak ditemukan: {reference_image}")
        if not reference_sha256:
            raise ComfyUIProviderError(
                "--reference-sha256 wajib agar gambar referensi terikat konfigurasi provider."
            )
        expected = _normalized_hash(reference_sha256)
        reference_hash = _sha256(reference_image)
        if reference_hash != expected:
            raise ComfyUIProviderError(
                "SHA-256 gambar referensi tidak sesuai: "
                f"expected={expected}, actual={reference_hash}"
            )
        reference_input = client.upload_image(reference_image, content_hash=reference_hash)
    elif reference_sha256:
        raise ComfyUIProviderError("--reference-sha256 memerlukan --reference-image.")

    workflow, output_node = build_workflow(
        payload,
        settings,
        reference_input=reference_input,
    )
    prompt_id = client.queue_prompt(workflow, client_id="hermes-" + uuid.uuid4().hex)
    image_info = client.wait_for_image(
        prompt_id,
        output_node=output_node,
        timeout=execution_timeout,
        poll_interval=poll_interval,
    )
    image_bytes = client.download_image(image_info)
    if not image_bytes.startswith(PNG_SIGNATURE):
        raise ComfyUIProviderError("Output ComfyUI bukan file PNG yang valid.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(image_bytes)
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "prompt_id": prompt_id,
        "output": str(output_path),
        "size_bytes": len(image_bytes),
        "reference_sha256": reference_hash,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes reference-image bridge for local ComfyUI.")
    parser.add_argument("--request")
    parser.add_argument("--output")
    parser.add_argument(
        "--server",
        default=os.environ.get("HERMES_COMFYUI_URL", "http://127.0.0.1:8188"),
    )
    parser.add_argument("--checkpoint", default="sd_xl_base_1.0.safetensors")
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--cfg", type=float, default=6.0)
    parser.add_argument("--sampler", default="dpmpp_2m")
    parser.add_argument("--scheduler", default="karras")
    parser.add_argument("--negative-prompt", default=DEFAULT_NEGATIVE_PROMPT)
    parser.add_argument("--prompt-suffix", default=DEFAULT_PROMPT_SUFFIX)
    parser.add_argument("--filename-prefix", default="HermesAI/provider-reference")
    parser.add_argument("--reference-image")
    parser.add_argument("--reference-sha256")
    parser.add_argument(
        "--clip-vision-model",
        default="CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors",
    )
    parser.add_argument(
        "--ipadapter-model",
        default="ip-adapter-plus_sdxl_vit-h.safetensors",
    )
    parser.add_argument("--ipadapter-weight", type=float, default=0.8)
    parser.add_argument("--ipadapter-start", type=float, default=0.0)
    parser.add_argument("--ipadapter-end", type=float, default=0.85)
    parser.add_argument("--execution-timeout", type=float, default=840.0)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--health-check", action="store_true")
    parser.add_argument("--require-ipadapter", action="store_true")
    return parser


def _settings(args: argparse.Namespace) -> GenerationSettings:
    if not 1 <= args.steps <= 200:
        raise ComfyUIProviderError("--steps harus berada pada rentang 1-200.")
    if not 0 < args.cfg <= 100:
        raise ComfyUIProviderError("--cfg harus > 0 dan <= 100.")
    if not 0 <= args.ipadapter_start < args.ipadapter_end <= 1:
        raise ComfyUIProviderError("Rentang IP-Adapter harus 0 <= start < end <= 1.")
    if not 0 <= args.ipadapter_weight <= 3:
        raise ComfyUIProviderError("--ipadapter-weight harus berada pada rentang 0-3.")
    if args.execution_timeout <= 0 or args.poll_interval <= 0:
        raise ComfyUIProviderError("Timeout dan poll interval harus lebih besar dari nol.")
    return GenerationSettings(
        checkpoint=args.checkpoint,
        steps=args.steps,
        cfg=args.cfg,
        sampler=args.sampler,
        scheduler=args.scheduler,
        negative_prompt=args.negative_prompt,
        prompt_suffix=args.prompt_suffix,
        filename_prefix=args.filename_prefix,
        clip_vision_model=args.clip_vision_model,
        ipadapter_model=args.ipadapter_model,
        ipadapter_weight=args.ipadapter_weight,
        ipadapter_start=args.ipadapter_start,
        ipadapter_end=args.ipadapter_end,
    )


def main() -> int:
    args = build_parser().parse_args()
    try:
        settings = _settings(args)
        client = ComfyUIClient(args.server)
        if args.health_check:
            result = client.healthcheck(
                settings,
                require_ipadapter=args.require_ipadapter or bool(args.reference_image),
            )
        else:
            if not args.request or not args.output:
                raise ComfyUIProviderError("--request dan --output wajib untuk generation.")
            result = generate_reference(
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
    except (ComfyUIProviderError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nProvider ComfyUI dihentikan oleh operator.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
