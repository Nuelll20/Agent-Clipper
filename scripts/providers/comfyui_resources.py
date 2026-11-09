#!/usr/bin/env python3
"""Release cached ComfyUI resources between heavy generation stages."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable


class ComfyUIResourceError(RuntimeError):
    """ComfyUI resources could not be released safely."""


class ComfyUIResourceManager:
    """Manage model and memory cleanup through the ComfyUI API."""

    def __init__(
        self,
        server: str,
        *,
        timeout: float = 30.0,
        urlopen: Callable[..., Any] | None = None,
    ) -> None:
        normalized_server = server.strip().rstrip("/")

        if not normalized_server:
            raise ValueError(
                "Alamat server ComfyUI tidak boleh kosong."
            )

        if timeout <= 0:
            raise ValueError(
                "Timeout pelepasan resource harus lebih dari nol."
            )

        self.server = normalized_server
        self.timeout = timeout
        self._urlopen = urlopen or urllib.request.urlopen

    def release(
        self,
        *,
        unload_models: bool = True,
        free_memory: bool = True,
    ) -> dict[str, Any]:
        """Unload cached models and release unused ComfyUI memory."""

        payload = {
            "unload_models": unload_models,
            "free_memory": free_memory,
        }

        request = urllib.request.Request(
            f"{self.server}/free",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        try:
            with self._urlopen(
                request,
                timeout=self.timeout,
            ) as response:
                status = int(
                    getattr(response, "status", 200)
                )
                body = response.read()

        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(
                "utf-8",
                errors="replace",
            ).strip()

            raise ComfyUIResourceError(
                "ComfyUI menolak pelepasan resource "
                f"(HTTP {exc.code}): "
                f"{detail or exc.reason}"
            ) from exc

        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
        ) as exc:
            reason = getattr(exc, "reason", exc)

            raise ComfyUIResourceError(
                "Gagal menghubungi ComfyUI untuk "
                f"melepaskan resource: {reason}"
            ) from exc

        if not 200 <= status < 300:
            raise ComfyUIResourceError(
                "ComfyUI mengembalikan status tidak valid "
                f"ketika melepas resource: HTTP {status}."
            )

        response_payload: Any = None

        if body:
            decoded_body = body.decode(
                "utf-8",
                errors="replace",
            ).strip()

            if decoded_body:
                try:
                    response_payload = json.loads(
                        decoded_body
                    )
                except json.JSONDecodeError:
                    response_payload = decoded_body

        return {
            "released": True,
            "server": self.server,
            "unload_models": unload_models,
            "free_memory": free_memory,
            "response": response_payload,
        }