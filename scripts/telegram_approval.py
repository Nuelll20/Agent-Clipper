#!/usr/bin/env python3
"""Send rendered clips to Telegram and record owner-only approval decisions.

This module intentionally uses only the Python standard library. Secrets are
loaded from the Hermes .env file and are never written to the approval DB.
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import mimetypes
import os
import secrets
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterable


APP_NAME = "Hermes Clip Approval"
CALLBACK_PREFIX = "hca"
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_REVISION = "revision_requested"


class ApprovalError(RuntimeError):
    """Expected configuration, storage, or Telegram API error."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def default_hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        return Path(r"D:\Hermes")
    return Path.home() / ".hermes"


def default_env_path() -> Path:
    configured = os.environ.get("HERMES_ENV", "").strip()
    return Path(configured).expanduser() if configured else default_hermes_home() / ".env"


def default_state_path() -> Path:
    configured = os.environ.get("HERMES_VIDEO_APPROVAL_DB", "").strip()
    if configured:
        return Path(configured).expanduser()
    project_root = Path(__file__).resolve().parent.parent
    return project_root / "state" / "telegram-approval.db"


def load_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ApprovalError(f"File konfigurasi tidak ditemukan: {path}")

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def merged_config(env_path: Path) -> dict[str, str]:
    values = load_dotenv(env_path)
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_OWNER_USER_ID"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    missing = [
        key
        for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_OWNER_USER_ID")
        if not values.get(key, "").strip()
    ]
    if missing:
        raise ApprovalError("Konfigurasi belum lengkap: " + ", ".join(missing))
    return values


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS clips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            job_id TEXT NOT NULL,
            clip_id TEXT NOT NULL,
            video_path TEXT NOT NULL,
            caption TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            chat_id TEXT,
            message_id INTEGER,
            decision_by TEXT,
            decision_at TEXT,
            error_message TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_clips_job_id ON clips(job_id);
        CREATE INDEX IF NOT EXISTS idx_clips_status ON clips(status);
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    connection.commit()
    return connection


def get_meta(connection: sqlite3.Connection, key: str, default: str = "") -> str:
    row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        """
        INSERT INTO metadata(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    connection.commit()


def telegram_json(
    bot_token: str,
    method: str,
    payload: dict[str, Any],
    timeout: int = 40,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    connection = http.client.HTTPSConnection("api.telegram.org", timeout=timeout)
    try:
        connection.request(
            "POST",
            f"/bot{bot_token}/{method}",
            body=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": str(len(body)),
                "User-Agent": "HermesClipApproval/1.0",
            },
        )
        response = connection.getresponse()
        raw = response.read()
    except (OSError, http.client.HTTPException) as exc:
        raise ApprovalError(f"Telegram tidak dapat dihubungi: {exc}") from None
    finally:
        connection.close()

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApprovalError("Telegram mengirim respons yang tidak dapat dibaca.") from None
    if response.status >= 400 or not data.get("ok"):
        description = str(data.get("description") or f"HTTP {response.status}")
        raise ApprovalError(f"Telegram menolak permintaan: {description}")
    return data


def _multipart_field(boundary: str, name: str, value: str) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n"
    ).encode("utf-8")


def telegram_upload_video(
    bot_token: str,
    video_path: Path,
    fields: dict[str, str],
    timeout: int = 300,
) -> dict[str, Any]:
    boundary = "----HermesClip" + uuid.uuid4().hex
    prefix_parts = [_multipart_field(boundary, key, value) for key, value in fields.items()]
    safe_name = video_path.name.replace('"', "_")
    mime_type = mimetypes.guess_type(video_path.name)[0] or "video/mp4"
    file_header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="video"; filename="{safe_name}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("utf-8")
    closing = f"\r\n--{boundary}--\r\n".encode("utf-8")
    prefix = b"".join(prefix_parts) + file_header
    content_length = len(prefix) + video_path.stat().st_size + len(closing)

    connection = http.client.HTTPSConnection("api.telegram.org", timeout=timeout)
    try:
        connection.putrequest("POST", f"/bot{bot_token}/sendVideo")
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", str(content_length))
        connection.putheader("User-Agent", "HermesClipApproval/1.0")
        connection.endheaders()
        connection.send(prefix)
        with video_path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                connection.send(chunk)
        connection.send(closing)
        response = connection.getresponse()
        raw = response.read()
    except (OSError, http.client.HTTPException) as exc:
        raise ApprovalError(f"Upload Telegram gagal: {exc}") from None
    finally:
        connection.close()

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApprovalError("Respons upload Telegram tidak dapat dibaca.") from None
    if response.status >= 400 or not data.get("ok"):
        description = str(data.get("description") or f"HTTP {response.status}")
        raise ApprovalError(f"Telegram menolak video: {description}")
    return data


def callback_data(action: str, token: str) -> str:
    value = f"{CALLBACK_PREFIX}:{action}:{token}"
    if len(value.encode("utf-8")) > 64:
        raise ApprovalError("Data tombol Telegram terlalu panjang.")
    return value


def approval_keyboard(token: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Setujui", "callback_data": callback_data("a", token)},
                {"text": "❌ Tolak", "callback_data": callback_data("r", token)},
            ],
            [{"text": "✏️ Revisi", "callback_data": callback_data("e", token)}],
        ]
    }


def compact_caption(caption: str, limit: int = 900) -> str:
    clean = "\n".join(line.rstrip() for line in caption.strip().splitlines()).strip()
    if not clean:
        clean = "Klip siap ditinjau."
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def create_clip_record(
    connection: sqlite3.Connection,
    job_id: str,
    clip_id: str,
    video_path: Path,
    caption: str,
) -> str:
    token = secrets.token_urlsafe(9)
    now = utc_now()
    connection.execute(
        """
        INSERT INTO clips(
            token, job_id, clip_id, video_path, caption, status,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            token,
            job_id,
            clip_id,
            str(video_path.resolve()),
            caption,
            STATUS_PENDING,
            now,
            now,
        ),
    )
    connection.commit()
    return token


def command_send(args: argparse.Namespace) -> int:
    video_path = Path(args.video).expanduser().resolve()
    if not video_path.is_file():
        raise ApprovalError(f"Video tidak ditemukan: {video_path}")
    if video_path.stat().st_size == 0:
        raise ApprovalError("Video kosong dan tidak dapat dikirim.")

    caption = args.caption or ""
    if args.caption_file:
        caption_path = Path(args.caption_file).expanduser().resolve()
        if not caption_path.is_file():
            raise ApprovalError(f"File caption tidak ditemukan: {caption_path}")
        caption = caption_path.read_text(encoding="utf-8-sig")
    caption = compact_caption(caption or f"{args.clip} siap ditinjau.")

    config = merged_config(Path(args.env).expanduser())
    connection = connect_db(Path(args.state).expanduser())
    try:
        token = create_clip_record(
            connection,
            job_id=args.job,
            clip_id=args.clip,
            video_path=video_path,
            caption=caption,
        )
        fields = {
            "chat_id": config["TELEGRAM_CHAT_ID"],
            "caption": caption,
            "supports_streaming": "true",
            "reply_markup": json.dumps(approval_keyboard(token), ensure_ascii=False),
        }
        try:
            response = telegram_upload_video(
                config["TELEGRAM_BOT_TOKEN"],
                video_path,
                fields,
                timeout=args.timeout,
            )
        except ApprovalError as exc:
            connection.execute(
                """
                UPDATE clips
                SET status = 'send_failed', updated_at = ?, error_message = ?
                WHERE token = ?
                """,
                (utc_now(), str(exc), token),
            )
            connection.commit()
            raise

        message = response["result"]
        connection.execute(
            """
            UPDATE clips
            SET chat_id = ?, message_id = ?, updated_at = ?, error_message = NULL
            WHERE token = ?
            """,
            (
                str(message["chat"]["id"]),
                int(message["message_id"]),
                utc_now(),
                token,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    print("Video terkirim: True")
    print(f"Job: {args.job}")
    print(f"Clip: {args.clip}")
    print("Status: menunggu persetujuan")
    return 0


def parse_callback(value: str) -> tuple[str, str] | None:
    parts = value.split(":", 2)
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:
        return None
    action, token = parts[1], parts[2]
    if action not in {"a", "r", "e"} or not token:
        return None
    return action, token


def action_details(action: str) -> tuple[str, str, str]:
    return {
        "a": (STATUS_APPROVED, "✅ DISETUJUI", "Klip disetujui."),
        "r": (STATUS_REJECTED, "❌ DITOLAK", "Klip ditolak."),
        "e": (STATUS_REVISION, "✏️ PERLU REVISI", "Klip ditandai untuk revisi."),
    }[action]


def answer_callback(bot_token: str, callback_id: str, text: str, alert: bool = False) -> None:
    telegram_json(
        bot_token,
        "answerCallbackQuery",
        {"callback_query_id": callback_id, "text": text, "show_alert": alert},
    )


def process_callback(
    connection: sqlite3.Connection,
    config: dict[str, str],
    callback: dict[str, Any],
) -> None:
    callback_id = str(callback.get("id", ""))
    sender_id = str((callback.get("from") or {}).get("id", ""))
    message = callback.get("message") or {}
    callback_chat_id = str((message.get("chat") or {}).get("id", ""))

    if sender_id != config["TELEGRAM_OWNER_USER_ID"] or callback_chat_id != config["TELEGRAM_CHAT_ID"]:
        if callback_id:
            answer_callback(
                config["TELEGRAM_BOT_TOKEN"],
                callback_id,
                "Tombol ini hanya untuk pemilik Hermes.",
                alert=True,
            )
        return

    parsed = parse_callback(str(callback.get("data", "")))
    if not parsed:
        if callback_id:
            answer_callback(config["TELEGRAM_BOT_TOKEN"], callback_id, "Tombol tidak dikenali.")
        return

    action, token = parsed
    row = connection.execute("SELECT * FROM clips WHERE token = ?", (token,)).fetchone()
    if not row:
        answer_callback(config["TELEGRAM_BOT_TOKEN"], callback_id, "Data klip tidak ditemukan.")
        return

    status, status_label, callback_text = action_details(action)
    if row["status"] != STATUS_PENDING:
        answer_callback(
            config["TELEGRAM_BOT_TOKEN"],
            callback_id,
            f"Klip sudah berstatus: {row['status']}",
        )
        return

    now = utc_now()
    cursor = connection.execute(
        """
        UPDATE clips
        SET status = ?, updated_at = ?, decision_by = ?, decision_at = ?
        WHERE token = ? AND status = ?
        """,
        (status, now, sender_id, now, token, STATUS_PENDING),
    )
    connection.commit()
    if cursor.rowcount != 1:
        answer_callback(config["TELEGRAM_BOT_TOKEN"], callback_id, "Status sudah berubah.")
        return

    answer_callback(config["TELEGRAM_BOT_TOKEN"], callback_id, callback_text)
    updated_caption = compact_caption(str(row["caption"]), limit=820) + f"\n\nStatus: {status_label}"
    try:
        telegram_json(
            config["TELEGRAM_BOT_TOKEN"],
            "editMessageCaption",
            {
                "chat_id": config["TELEGRAM_CHAT_ID"],
                "message_id": int(message["message_id"]),
                "caption": updated_caption,
                "reply_markup": {"inline_keyboard": []},
            },
        )
    except ApprovalError:
        telegram_json(
            config["TELEGRAM_BOT_TOKEN"],
            "sendMessage",
            {
                "chat_id": config["TELEGRAM_CHAT_ID"],
                "text": f"{row['job_id']} / {row['clip_id']} — {status_label}",
            },
        )
    print(f"Keputusan diterima: {row['job_id']} / {row['clip_id']} -> {status}")


def poll_once(
    connection: sqlite3.Connection,
    config: dict[str, str],
    timeout: int,
) -> int:
    last_update_id = int(get_meta(connection, "last_update_id", "0") or 0)
    payload: dict[str, Any] = {
        "timeout": timeout,
        "allowed_updates": ["callback_query"],
    }
    if last_update_id:
        payload["offset"] = last_update_id + 1
    response = telegram_json(
        config["TELEGRAM_BOT_TOKEN"],
        "getUpdates",
        payload,
        timeout=max(timeout + 15, 40),
    )
    processed = 0
    for update in response.get("result", []):
        update_id = int(update.get("update_id", 0))
        callback = update.get("callback_query")
        try:
            if callback:
                process_callback(connection, config, callback)
                processed += 1
        finally:
            if update_id:
                set_meta(connection, "last_update_id", str(update_id))
    return processed


def command_watch(args: argparse.Namespace) -> int:
    config = merged_config(Path(args.env).expanduser())
    connection = connect_db(Path(args.state).expanduser())
    print("Pemantau persetujuan aktif. Tekan Ctrl+C untuk berhenti.")
    try:
        while True:
            poll_once(connection, config, args.timeout)
            if args.once:
                break
            if args.pause:
                time.sleep(args.pause)
    except KeyboardInterrupt:
        print("\nPemantau dihentikan.")
    finally:
        connection.close()
    return 0


def _format_rows(rows: Iterable[sqlite3.Row]) -> None:
    rows = list(rows)
    if not rows:
        print("Belum ada data klip.")
        return
    print(f"{'JOB':20} {'CLIP':18} {'STATUS':20} {'UPDATED'}")
    print("-" * 82)
    for row in rows:
        print(
            f"{str(row['job_id'])[:20]:20} "
            f"{str(row['clip_id'])[:18]:18} "
            f"{str(row['status'])[:20]:20} "
            f"{row['updated_at']}"
        )


def command_status(args: argparse.Namespace) -> int:
    connection = connect_db(Path(args.state).expanduser())
    try:
        if args.job:
            rows = connection.execute(
                "SELECT * FROM clips WHERE job_id = ? ORDER BY id DESC",
                (args.job,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM clips ORDER BY id DESC LIMIT ?",
                (args.limit,),
            ).fetchall()
    finally:
        connection.close()
    _format_rows(rows)
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    env_path = Path(args.env).expanduser()
    config = merged_config(env_path)
    connection = connect_db(Path(args.state).expanduser())
    connection.close()
    response = telegram_json(config["TELEGRAM_BOT_TOKEN"], "getMe", {})
    username = str(response["result"].get("username", "bot"))
    print("Konfigurasi lengkap: True")
    print("Database siap: True")
    print("Telegram terhubung: True")
    print(f"Bot: @{username}")
    return 0


def command_self_test(_: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="hermes-approval-test-") as temp_dir:
        root = Path(temp_dir)
        env_path = root / ".env"
        env_path.write_text(
            "\ufeff# test\nTELEGRAM_BOT_TOKEN='123:test'\n"
            'TELEGRAM_CHAT_ID="456"\nTELEGRAM_OWNER_USER_ID=789\n',
            encoding="utf-8",
        )
        values = load_dotenv(env_path)
        assert values["TELEGRAM_BOT_TOKEN"] == "123:test"
        assert values["TELEGRAM_CHAT_ID"] == "456"
        assert values["TELEGRAM_OWNER_USER_ID"] == "789"

        db_path = root / "state" / "approval.db"
        connection = connect_db(db_path)
        video_path = root / "clip.mp4"
        video_path.write_bytes(b"test")
        token = create_clip_record(
            connection,
            "self-test-job",
            "clip-01",
            video_path,
            "Test caption",
        )
        parsed = parse_callback(callback_data("a", token))
        assert parsed == ("a", token)
        row = connection.execute("SELECT * FROM clips WHERE token = ?", (token,)).fetchone()
        assert row is not None and row["status"] == STATUS_PENDING
        set_meta(connection, "last_update_id", "123")
        assert get_meta(connection, "last_update_id") == "123"
        connection.close()

    print("SELF-TEST: OK")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Kirim klip Hermes ke Telegram dan catat keputusan pemilik.",
    )
    parser.add_argument("--env", default=str(default_env_path()), help="Lokasi file .env Hermes")
    parser.add_argument("--state", default=str(default_state_path()), help="Lokasi database persetujuan")
    subparsers = parser.add_subparsers(dest="command", required=True)

    send_parser = subparsers.add_parser("send", help="Kirim satu video untuk ditinjau")
    send_parser.add_argument("video", help="Path video MP4")
    send_parser.add_argument("--job", required=True, help="ID pekerjaan")
    send_parser.add_argument("--clip", required=True, help="ID klip")
    caption_group = send_parser.add_mutually_exclusive_group()
    caption_group.add_argument("--caption", help="Caption video")
    caption_group.add_argument("--caption-file", help="File teks berisi caption")
    send_parser.add_argument("--timeout", type=int, default=300, help="Timeout upload dalam detik")
    send_parser.set_defaults(handler=command_send)

    watch_parser = subparsers.add_parser("watch", help="Pantau tombol persetujuan")
    watch_parser.add_argument("--once", action="store_true", help="Lakukan satu kali polling")
    watch_parser.add_argument("--timeout", type=int, default=25, help="Long-poll timeout")
    watch_parser.add_argument("--pause", type=float, default=0.2, help="Jeda antar polling")
    watch_parser.set_defaults(handler=command_watch)

    status_parser = subparsers.add_parser("status", help="Lihat status klip")
    status_parser.add_argument("--job", help="Filter berdasarkan job")
    status_parser.add_argument("--limit", type=int, default=20, help="Jumlah data terbaru")
    status_parser.set_defaults(handler=command_status)

    doctor_parser = subparsers.add_parser("doctor", help="Periksa konfigurasi dan koneksi")
    doctor_parser.set_defaults(handler=command_doctor)

    test_parser = subparsers.add_parser("self-test", help="Jalankan pengujian lokal tanpa jaringan")
    test_parser.set_defaults(handler=command_self_test)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.handler(args) or 0)
    except ApprovalError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"ERROR database: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
