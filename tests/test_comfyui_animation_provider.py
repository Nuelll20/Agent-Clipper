import hashlib
import http.server
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.providers.comfyui_animation import (
    ComfyUIAnimationError,
    ComfyUIClient,
    GenerationSettings,
    build_workflow,
    generate_animation,
    resolve_reference,
    validate_request,
    wan_frame_count,
)


MP4_FIXTURE = b"\x00\x00\x00\x18ftypmp42" + b"fixture-mp4"
PNG_FIXTURE = b"\x89PNG\r\n\x1a\nfixture-png"


def hermes_request(reference_hash=None):
    reference_hash = reference_hash or "a" * 64
    return {
        "schema_version": "1.0",
        "story_id": "story-01",
        "revision_id": "rev-01",
        "production_id": "prod-01",
        "shot_id": "shot-01",
        "kind": "animation",
        "input_fingerprint": "sha256:" + "b" * 64,
        "dependency_artifacts": ["sha256:" + reference_hash],
        "request": {
            "shot_id": "shot-01",
            "scene_id": "scene-01",
            "duration_seconds": 2.0,
            "width": 720,
            "height": 1280,
            "fps": 24.0,
            "characters": [
                {
                    "id": "ari",
                    "name": "Ari",
                    "visual_identity": "short black bob hair and cream utility jacket",
                }
            ],
            "location": {
                "id": "tunnel",
                "name": "Old tunnel",
                "description": "an abandoned underground station",
            },
            "prompt": "Ari raises her lantern and takes one careful step",
            "camera": "slow cinematic push-in",
            "continuity_notes": ["keep the jacket and face stable"],
            "reference_dependency": "reference",
            "seed": 1234,
        },
    }


def write_reference_attempt(base: Path, payload: dict):
    output_path = base / "attempts" / "shot-01" / "reference" / "attempt-ref" / "output.png"
    output_path.parent.mkdir(parents=True)
    output_path.write_bytes(PNG_FIXTURE)
    fingerprint = hashlib.sha256(PNG_FIXTURE).hexdigest()
    attempt = {
        "story_id": "story-01",
        "revision_id": "rev-01",
        "production_id": "prod-01",
        "shot_id": "shot-01",
        "kind": "reference",
        "state": "COMPLETED",
        "output": {
            "path": output_path.relative_to(base).as_posix(),
            "fingerprint": "sha256:" + fingerprint,
            "size_bytes": len(PNG_FIXTURE),
        },
    }
    (output_path.parent / "attempt.json").write_text(json.dumps(attempt), encoding="utf-8")
    payload["dependency_artifacts"] = ["sha256:" + fingerprint]
    request_path = (
        base / "attempts" / "shot-01" / "animation" / "attempt-animation" / "request.json"
    )
    request_path.parent.mkdir(parents=True)
    request_path.write_text(json.dumps(payload), encoding="utf-8")
    return request_path, output_path, fingerprint


class RequestAndWorkflowTests(unittest.TestCase):
    def test_builds_wan_workflow_and_caps_frames_for_local_policy(self):
        workflow, output_node, metadata = build_workflow(
            hermes_request(),
            GenerationSettings(generation_width=416, generation_height=736, max_frames=49),
            reference_input="HermesAI/provider-inputs/reference.png",
        )

        self.assertEqual(output_node, "13")
        self.assertEqual(workflow["1"]["class_type"], "UNETLoader")
        self.assertEqual(workflow["2"]["inputs"]["type"], "wan")
        self.assertEqual(workflow["8"]["inputs"]["width"], 416)
        self.assertEqual(workflow["8"]["inputs"]["height"], 736)
        self.assertEqual(workflow["8"]["inputs"]["length"], 49)
        self.assertEqual(workflow["11"]["inputs"]["width"], 720)
        self.assertEqual(workflow["11"]["inputs"]["height"], 1280)
        self.assertEqual(workflow["13"]["inputs"]["format"], "mp4")
        self.assertEqual(workflow["13"]["inputs"]["codec"], "h264")
        self.assertIn("slow cinematic push-in", workflow["6"]["inputs"]["text"])
        self.assertFalse(metadata["duration_clamped"])

    def test_frame_count_is_four_n_plus_one_and_can_be_capped(self):
        self.assertEqual(wan_frame_count(0.7, 24, 49), 17)
        self.assertEqual(wan_frame_count(2.0, 24, 49), 49)
        self.assertEqual(wan_frame_count(8.0, 24, 49), 49)

    def test_rejects_invalid_animation_requests(self):
        cases = [
            (lambda value: value.update(kind="reference"), "animation"),
            (lambda value: value.update(dependency_artifacts=[]), "dependency"),
            (lambda value: value["request"].update(reference_dependency="image"), "reference"),
            (lambda value: value["request"].update(prompt=""), "prompt"),
            (lambda value: value["request"].update(fps=0), "fps"),
            (lambda value: value["request"].update(seed=-1), "seed"),
        ]
        for mutation, message in cases:
            with self.subTest(message=message):
                payload = hermes_request()
                mutation(payload)
                with self.assertRaisesRegex(ComfyUIAnimationError, message):
                    validate_request(payload)


class ReferenceResolutionTests(unittest.TestCase):
    def test_resolves_reference_by_attempt_fingerprint(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            payload = hermes_request()
            request_path, reference_path, fingerprint = write_reference_attempt(base, payload)

            resolved, actual = resolve_reference(request_path, payload)

            self.assertEqual(resolved, reference_path)
            self.assertEqual(actual, fingerprint)

    def test_blocks_reference_file_tampering(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            payload = hermes_request()
            request_path, reference_path, _ = write_reference_attempt(base, payload)
            reference_path.write_bytes(PNG_FIXTURE + b"tampered")

            with self.assertRaisesRegex(ComfyUIAnimationError, "Tidak ada completed"):
                resolve_reference(request_path, payload)


class FakeClient:
    def __init__(self):
        self.workflow = None
        self.uploaded = None

    def upload_image(self, source, *, content_hash):
        self.uploaded = (source, content_hash)
        return "HermesAI/provider-inputs/uploaded.png"

    def queue_prompt(self, workflow, *, client_id):
        self.workflow = workflow
        assert client_id.startswith("hermes-")
        return "prompt-01"

    def wait_for_video(self, prompt_id, *, output_node, timeout, poll_interval):
        assert prompt_id == "prompt-01"
        assert output_node == "13"
        return {"filename": "result.mp4", "subfolder": "HermesAI", "type": "output"}

    def download_output(self, info):
        return MP4_FIXTURE


class GenerationTests(unittest.TestCase):
    def test_generation_writes_verified_mp4_atomically(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            payload = hermes_request()
            request_path, reference_path, reference_hash = write_reference_attempt(base, payload)
            output_path = request_path.parent / "output.mp4"
            client = FakeClient()

            result = generate_animation(
                request_path,
                output_path,
                client=client,
                settings=GenerationSettings(generation_width=416, generation_height=736),
                execution_timeout=60,
                poll_interval=0.01,
            )

            self.assertEqual(output_path.read_bytes(), MP4_FIXTURE)
            self.assertEqual(result["prompt_id"], "prompt-01")
            self.assertEqual(result["reference_sha256"], reference_hash)
            self.assertEqual(client.uploaded, (reference_path, reference_hash))
            self.assertEqual(client.workflow["8"]["class_type"], "Wan22ImageToVideoLatent")

    def test_http_client_roundtrip_health_upload_history_and_download(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            workflow = None
            upload_body = b""

            def log_message(self, format, *args):
                return

            def _send(self, payload, content_type="application/json"):
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                if self.path == "/upload/image":
                    type(self).upload_body = body
                    self._send(
                        {
                            "name": "uploaded.png",
                            "subfolder": "HermesAI/provider-inputs",
                            "type": "input",
                        }
                    )
                    return
                if self.path == "/prompt":
                    type(self).workflow = json.loads(body)["prompt"]
                    self._send({"prompt_id": "prompt-http", "node_errors": {}})
                    return
                self.send_error(404)

            def do_GET(self):
                if self.path == "/system_stats":
                    self._send({"system": {"comfyui_version": "test"}, "devices": ["cuda:0"]})
                    return
                if self.path == "/object_info":
                    nodes = {
                        name: {"input": {"required": {}}}
                        for name in (
                            "LoadImage",
                            "ModelSamplingSD3",
                            "Wan22ImageToVideoLatent",
                            "KSampler",
                            "VAEDecode",
                            "ImageScale",
                            "CreateVideo",
                            "SaveVideo",
                        )
                    }
                    nodes.update(
                        {
                            "UNETLoader": {
                                "input": {"required": {"unet_name": [["wan.safetensors"]]}}
                            },
                            "CLIPLoader": {
                                "input": {"required": {"clip_name": [["umt5.safetensors"]]}}
                            },
                            "VAELoader": {
                                "input": {"required": {"vae_name": [["wan-vae.safetensors"]]}}
                            },
                        }
                    )
                    self._send(nodes)
                    return
                if self.path == "/history/prompt-http":
                    self._send(
                        {
                            "prompt-http": {
                                "status": {"status_str": "success", "completed": True},
                                "outputs": {
                                    "13": {
                                        "video": [
                                            {
                                                "filename": "result.mp4",
                                                "subfolder": "HermesAI",
                                                "type": "output",
                                            }
                                        ]
                                    }
                                },
                            }
                        }
                    )
                    return
                if self.path.startswith("/view?"):
                    self._send(MP4_FIXTURE, "video/mp4")
                    return
                self.send_error(404)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = ComfyUIClient(f"http://127.0.0.1:{server.server_address[1]}", timeout=2)
            settings = GenerationSettings(
                diffusion_model="wan.safetensors",
                text_encoder="umt5.safetensors",
                vae="wan-vae.safetensors",
            )
            health = client.healthcheck(settings)
            with TemporaryDirectory() as directory:
                image = Path(directory) / "reference.png"
                image.write_bytes(PNG_FIXTURE)
                uploaded = client.upload_image(
                    image,
                    content_hash=hashlib.sha256(PNG_FIXTURE).hexdigest(),
                )
            prompt_id = client.queue_prompt({"13": {}}, client_id="hermes-test")
            info = client.wait_for_video(
                prompt_id, output_node="13", timeout=2, poll_interval=0.01
            )
            output = client.download_output(info)

            self.assertTrue(health["ready"])
            self.assertEqual(uploaded, "HermesAI/provider-inputs/uploaded.png")
            self.assertEqual(output, MP4_FIXTURE)
            self.assertIn(b"animation-reference-", Handler.upload_body)
            self.assertEqual(Handler.workflow, {"13": {}})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
