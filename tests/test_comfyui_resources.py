import json
import urllib.error

import pytest

from scripts.providers.comfyui_resources import (
    ComfyUIResourceError,
    ComfyUIResourceManager,
)


class FakeResponse:
    def __init__(
        self,
        body: bytes = b"{}",
        status: int = 200,
    ) -> None:
        self.body = body
        self.status = status

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(
        self,
        exc_type,
        exc_value,
        traceback,
    ) -> None:
        return None


def test_release_posts_expected_payload():
    captured = {}

    def fake_urlopen(request, *, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["payload"] = json.loads(
            request.data.decode("utf-8")
        )
        captured["content_type"] = request.get_header(
            "Content-type"
        )
        captured["timeout"] = timeout

        return FakeResponse()

    manager = ComfyUIResourceManager(
        "http://127.0.0.1:8188/",
        timeout=15,
        urlopen=fake_urlopen,
    )

    result = manager.release()

    assert captured == {
        "url": "http://127.0.0.1:8188/free",
        "method": "POST",
        "payload": {
            "unload_models": True,
            "free_memory": True,
        },
        "content_type": "application/json",
        "timeout": 15,
    }

    assert result["released"] is True
    assert result["server"] == "http://127.0.0.1:8188"
    assert result["unload_models"] is True
    assert result["free_memory"] is True


def test_release_wraps_connection_error():
    def failing_urlopen(request, *, timeout):
        raise urllib.error.URLError(
            "synthetic connection failure"
        )

    manager = ComfyUIResourceManager(
        "http://127.0.0.1:8188",
        urlopen=failing_urlopen,
    )

    with pytest.raises(
        ComfyUIResourceError,
        match="synthetic connection failure",
    ):
        manager.release()


@pytest.mark.parametrize(
    ("server", "timeout"),
    [
        ("", 30),
        ("   ", 30),
        ("http://127.0.0.1:8188", 0),
        ("http://127.0.0.1:8188", -1),
    ],
)
def test_rejects_invalid_configuration(
    server,
    timeout,
):
    with pytest.raises(ValueError):
        ComfyUIResourceManager(
            server,
            timeout=timeout,
        )