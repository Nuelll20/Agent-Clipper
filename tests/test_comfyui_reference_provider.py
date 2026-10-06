import hashlib
import http.server
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.providers.comfyui_reference import (
    PNG_SIGNATURE,
    ComfyUIClient,
    ComfyUIProviderError,
    GenerationSettings,
    build_workflow,
    generate_reference,
    validate_request,
)


def hermes_request():
    return {
        "schema_version": "1.0",
        "story_id": "story-01",
        "revision_id": "rev-01",
        "production_id": "prod-01",
        "shot_id": "shot-01",
        "kind": "reference",
        "input_fingerprint": "sha256:" + "a" * 64,
        "dependency_artifacts": [],
        "request": {
            "shot_id": "shot-01",
            "width": 720,
            "height": 1280,
            "seed": 1234,
            "prompt": "Ari berjalan di terowongan tua",
        },
    }


class WorkflowTests(unittest.TestCase):
    def test_builds_sdxl_workflow_from_hermes_request(self):
        workflow, output_node = build_workflow(
            hermes_request(),
            GenerationSettings(checkpoint="sd_xl_base_1.0.safetensors"),
        )

        self.assertEqual(output_node, "7")
        self.assertEqual(
            workflow["1"]["inputs"]["ckpt_name"],
            "sd_xl_base_1.0.safetensors",
        )
        self.assertEqual(
            workflow["4"]["inputs"],
            {"width": 720, "height": 1280, "batch_size": 1},
        )
        self.assertEqual(workflow["5"]["inputs"]["seed"], 1234)
        self.assertEqual(workflow["5"]["inputs"]["model"], ["1", 0])
        self.assertIn("Ari berjalan", workflow["2"]["inputs"]["text"])
        self.assertTrue(
            workflow["7"]["inputs"]["filename_prefix"].endswith(
                "shot-01-aaaaaaaaaaaa"
            )
        )

    def test_builds_ipadapter_workflow_when_reference_is_uploaded(self):
        workflow, output_node = build_workflow(
            hermes_request(),
            GenerationSettings(checkpoint="sdxl.safetensors", ipadapter_weight=0.75),
            reference_input="HermesAI/provider-inputs/reference.png",
        )

        self.assertEqual(output_node, "11")
        self.assertEqual(workflow["2"]["class_type"], "LoadImage")
        self.assertEqual(workflow["5"]["class_type"], "IPAdapterAdvanced")
        self.assertEqual(workflow["5"]["inputs"]["weight"], 0.75)
        self.assertEqual(workflow["9"]["inputs"]["model"], ["5", 0])

    def test_rejects_invalid_hermes_requests(self):
        cases = [
            (lambda value: value.update(kind="animation"), "reference"),
            (lambda value: value["request"].update(width=721), "width"),
            (lambda value: value["request"].update(prompt=""), "prompt"),
            (lambda value: value["request"].update(seed=-1), "seed"),
        ]
        for mutation, message in cases:
            with self.subTest(message=message):
                payload = hermes_request()
                mutation(payload)
                with self.assertRaisesRegex(ComfyUIProviderError, message):
                    validate_request(payload)


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

    def wait_for_image(self, prompt_id, *, output_node, timeout, poll_interval):
        assert prompt_id == "prompt-01"
        assert output_node == "11"
        return {"filename": "result.png", "subfolder": "HermesAI", "type": "output"}

    def download_image(self, image):
        return PNG_SIGNATURE + b"fixture-png"


class GenerationTests(unittest.TestCase):
    def test_generation_verifies_reference_and_writes_output_atomically(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            request_path = base / "request.json"
            request_path.write_text(json.dumps(hermes_request()), encoding="utf-8")
            reference_path = base / "ara.png"
            reference_path.write_bytes(PNG_SIGNATURE + b"source")
            reference_hash = hashlib.sha256(reference_path.read_bytes()).hexdigest()
            output_path = base / "output.png"
            client = FakeClient()

            result = generate_reference(
                request_path,
                output_path,
                client=client,
                settings=GenerationSettings(checkpoint="sdxl.safetensors"),
                execution_timeout=60,
                poll_interval=0.01,
                reference_image=reference_path,
                reference_sha256=reference_hash,
            )

            self.assertEqual(output_path.read_bytes(), PNG_SIGNATURE + b"fixture-png")
            self.assertEqual(result["prompt_id"], "prompt-01")
            self.assertEqual(result["reference_sha256"], reference_hash)
            self.assertEqual(client.uploaded, (reference_path, reference_hash))
            self.assertEqual(client.workflow["5"]["class_type"], "IPAdapterAdvanced")

    def test_http_client_roundtrip_including_upload_and_download(self):
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
                    self._send({
                        "name": "uploaded.png",
                        "subfolder": "HermesAI/provider-inputs",
                        "type": "input",
                    })
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
                if self.path == "/object_info/CheckpointLoaderSimple":
                    self._send({
                        "CheckpointLoaderSimple": {
                            "input": {"required": {"ckpt_name": [["sdxl.safetensors"]]}}
                        }
                    })
                    return
                if self.path == "/object_info/CLIPVisionLoader":
                    self._send({
                        "CLIPVisionLoader": {
                            "input": {"required": {"clip_name": [[
                                "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors"
                            ]]}}
                        }
                    })
                    return
                if self.path == "/object_info/IPAdapterModelLoader":
                    self._send({
                        "IPAdapterModelLoader": {
                            "input": {"required": {"ipadapter_file": [[
                                "ip-adapter-plus_sdxl_vit-h.safetensors"
                            ]]}}
                        }
                    })
                    return
                if self.path == "/object_info/IPAdapterAdvanced":
                    self._send({"IPAdapterAdvanced": {"input": {"required": {}}}})
                    return
                if self.path == "/history/prompt-http":
                    self._send({
                        "prompt-http": {
                            "status": {"status_str": "success", "completed": True},
                            "outputs": {
                                "11": {
                                    "images": [{
                                        "filename": "result.png",
                                        "subfolder": "HermesAI",
                                        "type": "output",
                                    }]
                                }
                            },
                        }
                    })
                    return
                if self.path.startswith("/view?"):
                    self._send(PNG_SIGNATURE + b"http-png", "image/png")
                    return
                self.send_error(404)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with TemporaryDirectory() as directory:
                base = Path(directory)
                request_path = base / "request.json"
                request_path.write_text(json.dumps(hermes_request()), encoding="utf-8")
                reference_path = base / "ara.png"
                reference_path.write_bytes(PNG_SIGNATURE + b"source")
                reference_hash = hashlib.sha256(reference_path.read_bytes()).hexdigest()
                output_path = base / "output.png"
                client = ComfyUIClient(
                    f"http://127.0.0.1:{server.server_address[1]}",
                    timeout=2,
                )
                health = client.healthcheck(
                    GenerationSettings(checkpoint="sdxl.safetensors"),
                    require_ipadapter=True,
                )

                result = generate_reference(
                    request_path,
                    output_path,
                    client=client,
                    settings=GenerationSettings(checkpoint="sdxl.safetensors"),
                    execution_timeout=2,
                    poll_interval=0.01,
                    reference_image=reference_path,
                    reference_sha256=reference_hash,
                )

                self.assertEqual(result["prompt_id"], "prompt-http")
                self.assertTrue(health["ready"])
                self.assertTrue(health["ipadapter"])
                self.assertEqual(output_path.read_bytes(), PNG_SIGNATURE + b"http-png")
                self.assertIn(b"reference-", Handler.upload_body)
                self.assertEqual(Handler.workflow["5"]["class_type"], "IPAdapterAdvanced")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_generation_blocks_reference_hash_mismatch(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            request_path = base / "request.json"
            request_path.write_text(json.dumps(hermes_request()), encoding="utf-8")
            reference_path = base / "ara.png"
            reference_path.write_bytes(PNG_SIGNATURE + b"source")

            with self.assertRaisesRegex(ComfyUIProviderError, "tidak sesuai"):
                generate_reference(
                    request_path,
                    base / "output.png",
                    client=FakeClient(),
                    settings=GenerationSettings(checkpoint="sdxl.safetensors"),
                    execution_timeout=60,
                    poll_interval=0.01,
                    reference_image=reference_path,
                    reference_sha256="0" * 64,
                )


if __name__ == "__main__":
    unittest.main()
