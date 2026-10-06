"""Shot-level asset production with immutable attempts and selective regeneration."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from core.artifacts import (
    CONTRACT_SCHEMA_VERSION,
    PRODUCER_VERSION,
    load_schema,
    sha256_file,
    sha256_json,
)
from core.story import (
    StoryGateError,
    assert_story_approved,
    safe_story_id,
    validate_story_revision,
)
from validators.artifact_validator import (
    ArtifactValidationError,
    load_json_object,
    validate_payload,
)


ASSET_ORDER = ("reference", "animation", "voice", "sfx")
ASSET_AGENTS = {
    "reference": "reference-artist",
    "animation": "animator",
    "voice": "voice-director",
    "sfx": "sound-designer",
}
FIXTURE_EXTENSIONS = {
    "reference": ".png",
    "animation": ".mp4",
    "voice": ".wav",
    "sfx": ".wav",
}


class ProductionError(RuntimeError):
    """Expected production-plan, manifest, or provider failure."""


class ProductionGateError(ProductionError):
    """A lineage, dependency, or approval rule blocked asset generation."""


class AssetProviderError(ProductionError):
    """A local asset provider did not create a usable output."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ProductionError(f"Artifact produksi tidak dapat ditulis: {path} ({exc})") from None


def _clean_error(value: Any) -> str:
    return " ".join(str(value).replace("\x00", "").split())[:1000]


def _safe_id(value: Any, label: str) -> str:
    try:
        return safe_story_id(value, label=label)
    except ArtifactValidationError as exc:
        raise ProductionGateError(str(exc)) from None


def _relative_path(base: Path, target: Path) -> str:
    try:
        return str(target.resolve().relative_to(base.resolve()))
    except ValueError:
        raise ProductionGateError(f"Artifact berada di luar production directory: {target}") from None


def _resolved_child(base: Path, relative: str, label: str) -> Path:
    path = (base / str(relative)).resolve()
    try:
        path.relative_to(base.resolve())
    except ValueError:
        raise ProductionGateError(f"{label} keluar dari production directory.") from None
    return path


def _story_context(manifest_path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest = validate_story_revision(manifest_path)
    context: dict[str, dict[str, Any]] = {}
    for entry in manifest["artifacts"]:
        artifact = load_json_object(manifest_path.parent / entry["path"], entry["stage"])
        context[entry["stage"]] = artifact["content"]
    return manifest, context


def _require_story_approval(manifest_path: Path) -> dict[str, Any]:
    try:
        return assert_story_approved(manifest_path)
    except (StoryGateError, ArtifactValidationError) as exc:
        raise ProductionGateError(str(exc)) from None


def validate_voice_cast(
    payload: dict[str, Any],
    *,
    manifest_path: str | Path,
) -> dict[str, Any]:
    manifest_path = Path(manifest_path).expanduser().resolve()
    manifest, context = _story_context(manifest_path)
    validate_payload(payload, load_schema("voice-cast.schema.json"), label="Voice cast")
    expected_identity = (manifest["story_id"], manifest["revision_id"])
    if (payload["story_id"], payload["revision_id"]) != expected_identity:
        raise ProductionGateError("Voice cast tidak cocok dengan story revision.")
    if payload["story_manifest_fingerprint"] != sha256_file(manifest_path):
        raise ProductionGateError("Story manifest berubah setelah voice cast dibuat.")
    character_ids = {item["id"] for item in context["universe"]["characters"]}
    cast_ids = [item["character_id"] for item in payload["voices"]]
    if len(set(cast_ids)) != len(cast_ids):
        raise ProductionGateError("Voice cast memiliki character_id duplikat.")
    if set(cast_ids) != character_ids:
        missing = sorted(character_ids - set(cast_ids))
        unknown = sorted(set(cast_ids) - character_ids)
        raise ProductionGateError(
            f"Voice cast harus mencakup tepat semua karakter; missing={missing}, unknown={unknown}."
        )
    for voice in payload["voices"]:
        reference_path = voice["reference_path"]
        reference_fingerprint = voice["reference_fingerprint"]
        if (reference_path is None) != (reference_fingerprint is None):
            raise ProductionGateError("Voice reference path dan fingerprint harus berpasangan.")
        if reference_path is not None:
            path = Path(reference_path).expanduser().resolve()
            if not path.is_file() or sha256_file(path) != reference_fingerprint:
                raise ProductionGateError(
                    f"Voice reference berubah atau tidak ditemukan: {voice['character_id']}"
                )
    return payload


def create_voice_cast(
    manifest_path: str | Path,
    assignments: dict[str, dict[str, Any]],
    output_path: str | Path,
) -> Path:
    manifest_path = Path(manifest_path).expanduser().resolve()
    _require_story_approval(manifest_path)
    manifest, context = _story_context(manifest_path)
    character_ids = {item["id"] for item in context["universe"]["characters"]}
    if set(assignments) != character_ids:
        missing = sorted(character_ids - set(assignments))
        unknown = sorted(set(assignments) - character_ids)
        raise ProductionGateError(
            f"Assignment suara harus tepat untuk semua karakter; missing={missing}, unknown={unknown}."
        )
    voices: list[dict[str, Any]] = []
    for character_id in sorted(character_ids):
        assignment = assignments[character_id]
        reference_value = assignment.get("reference_path")
        reference_path = (
            str(Path(reference_value).expanduser().resolve()) if reference_value else None
        )
        voices.append({
            "character_id": character_id,
            "provider": str(assignment.get("provider") or "").strip(),
            "provider_voice_id": str(assignment.get("provider_voice_id") or "").strip(),
            "language": str(assignment.get("language") or "").strip(),
            "style": str(assignment.get("style") or "").strip(),
            "reference_path": reference_path,
            "reference_fingerprint": sha256_file(reference_path) if reference_path else None,
        })
    payload = {
        "artifact_type": "voice-cast",
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "producer_version": PRODUCER_VERSION,
        "story_id": manifest["story_id"],
        "revision_id": manifest["revision_id"],
        "story_manifest_fingerprint": sha256_file(manifest_path),
        "created_at": utc_now(),
        "voices": voices,
    }
    validate_voice_cast(payload, manifest_path=manifest_path)
    output = Path(output_path).expanduser().resolve()
    if output.exists():
        raise ProductionError(f"Voice cast sudah ada dan tidak akan ditimpa: {output}")
    _atomic_write_json(output, payload)
    return output


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).casefold()
            if any(token in normalized for token in ("secret", "password", "api_key", "token")):
                return True
            if _contains_secret_key(nested):
                return True
    if isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


def load_provider_config(
    mode: str,
    config_path: str | Path | None = None,
) -> tuple[dict[str, Any], str, Path | None]:
    if mode == "fixture":
        config = {
            "artifact_type": "asset-provider-config",
            "schema_version": "1.0",
            "providers": {
                kind: {
                    "name": "fixture",
                    "command": ["fixture"],
                    "output_extension": FIXTURE_EXTENSIONS[kind],
                    "timeout_seconds": 30,
                }
                for kind in ASSET_ORDER
            },
        }
        return config, sha256_json(config), None
    if mode != "command":
        raise ProductionGateError(f"Provider mode tidak didukung: {mode}")
    if config_path is None:
        raise ProductionGateError("Provider mode command memerlukan config path.")
    path = Path(config_path).expanduser().resolve()
    config = load_json_object(path, "Asset provider config")
    validate_payload(
        config,
        load_schema("asset-provider-config.schema.json"),
        label="Asset provider config",
    )
    if _contains_secret_key(config):
        raise ProductionGateError(
            "Provider config tidak boleh menyimpan secret; gunakan environment provider."
        )
    return config, sha256_file(path), path


class AssetProvider(Protocol):
    def output_extension(self, kind: str) -> str: ...

    def describe(self, kind: str) -> dict[str, Any]: ...

    def generate(
        self,
        kind: str,
        request: dict[str, Any],
        request_path: Path,
        output_path: Path,
    ) -> None: ...


class FixtureAssetProvider:
    """Writes deterministic non-media bytes for offline orchestration tests only."""

    def output_extension(self, kind: str) -> str:
        return FIXTURE_EXTENSIONS[kind]

    def describe(self, kind: str) -> dict[str, Any]:
        return {"mode": "fixture", "kind": kind, "version": 1}

    def generate(
        self,
        kind: str,
        request: dict[str, Any],
        request_path: Path,
        output_path: Path,
    ) -> None:
        payload = json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")
        output_path.write_bytes(b"HERMES_FIXTURE_ASSET\n" + kind.encode("ascii") + b"\n" + payload)


class CommandAssetProvider:
    """Runs configured local executables without a shell or stored credentials."""

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def _entry(self, kind: str) -> dict[str, Any]:
        try:
            return self.config["providers"][kind]
        except KeyError:
            raise AssetProviderError(f"Provider command tidak tersedia untuk {kind}.") from None

    def output_extension(self, kind: str) -> str:
        return self._entry(kind)["output_extension"]

    def describe(self, kind: str) -> dict[str, Any]:
        entry = self._entry(kind)
        return {
            "mode": "command",
            "kind": kind,
            "name": entry["name"],
            "output_extension": entry["output_extension"],
        }

    def generate(
        self,
        kind: str,
        request: dict[str, Any],
        request_path: Path,
        output_path: Path,
    ) -> None:
        entry = self._entry(kind)
        command = [
            str(part).replace("{request}", str(request_path)).replace("{output}", str(output_path))
            for part in entry["command"]
        ]
        if not any("{request}" in str(part) for part in entry["command"]):
            command.extend(("--request", str(request_path)))
        if not any("{output}" in str(part) for part in entry["command"]):
            command.extend(("--output", str(output_path)))
        environment = os.environ.copy()
        environment.update({
            "HERMES_ASSET_KIND": kind,
            "HERMES_ASSET_REQUEST": str(request_path),
            "HERMES_ASSET_OUTPUT": str(output_path),
        })
        try:
            result = subprocess.run(
                command,
                cwd=str(output_path.parent),
                env=environment,
                timeout=float(entry["timeout_seconds"]),
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AssetProviderError(
                f"Provider {entry['name']} tidak dapat dijalankan ({type(exc).__name__})."
            ) from None
        if result.returncode != 0:
            raise AssetProviderError(
                f"Provider {entry['name']} untuk {kind} keluar dengan code {result.returncode}."
            )


def build_provider(mode: str, config: dict[str, Any]) -> AssetProvider:
    if mode == "fixture":
        return FixtureAssetProvider()
    if mode == "command":
        return CommandAssetProvider(config)
    raise ProductionGateError(f"Provider mode tidak didukung: {mode}")


def _voice_by_character(voice_cast: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["character_id"]: item for item in voice_cast["voices"]}


def _dialogue_character(
    shot: dict[str, Any],
    screenplay: dict[str, Any],
) -> str | None:
    dialogue = str(shot.get("dialogue") or "").strip()
    if not dialogue:
        return None
    candidates: set[str] = set()
    for scene in screenplay["scenes"]:
        if scene["id"] != shot["scene_id"]:
            continue
        for beat in scene["beats"]:
            if (
                str(beat.get("type") or "").upper() == "DIALOGUE"
                and str(beat.get("text") or "").strip() == dialogue
                and beat.get("character")
            ):
                candidates.add(beat["character"])
    if len(candidates) == 1:
        return next(iter(candidates))
    shot_characters = list(dict.fromkeys(shot.get("characters") or []))
    if not candidates and len(shot_characters) == 1:
        return shot_characters[0]
    raise ProductionGateError(
        f"Pembicara dialog shot {shot['id']} ambigu; perjelas screenplay atau shot list."
    )


def _stable_seed(*values: str) -> int:
    digest = sha256_json(list(values)).split(":", 1)[1]
    return int(digest[:8], 16)


def _asset_requests(
    manifest: dict[str, Any],
    context: dict[str, dict[str, Any]],
    voice_cast: dict[str, Any],
    provider: AssetProvider,
    *,
    width: int,
    height: int,
    fps: float,
) -> list[dict[str, Any]]:
    universe = context["universe"]
    screenplay = context["screenplay"]
    characters = {item["id"]: item for item in universe["characters"]}
    locations = {item["id"]: item for item in universe["locations"]}
    voices = _voice_by_character(voice_cast)
    plans: list[dict[str, Any]] = []
    for shot in context["shot-list"]["shots"]:
        shot_id = shot["id"]
        identities = [characters[item] for item in shot["characters"]]
        location = locations[shot["location_id"]]
        common = {
            "shot_id": shot_id,
            "scene_id": shot["scene_id"],
            "duration_seconds": shot["duration_seconds"],
            "width": width,
            "height": height,
            "fps": fps,
            "characters": [
                {"id": item["id"], "name": item["name"], "visual_identity": item["visual_identity"]}
                for item in identities
            ],
            "location": {
                "id": location["id"],
                "name": location["name"],
                "description": location["description"],
            },
        }
        assets = [
            {
                "kind": "reference",
                "agent_id": ASSET_AGENTS["reference"],
                "output_extension": provider.output_extension("reference"),
                "request": {
                    **common,
                    "prompt": (
                        f"{shot['framing']}; {shot['action']}; location: {location['description']}; "
                        + "; ".join(item["visual_identity"] for item in identities)
                    ),
                    "continuity_notes": shot["continuity_notes"],
                    "seed": _stable_seed(manifest["story_id"], manifest["revision_id"], shot_id, "reference"),
                },
            },
            {
                "kind": "animation",
                "agent_id": ASSET_AGENTS["animation"],
                "output_extension": provider.output_extension("animation"),
                "request": {
                    **common,
                    "prompt": shot["action"],
                    "camera": shot["camera"],
                    "continuity_notes": shot["continuity_notes"],
                    "reference_dependency": "reference",
                    "seed": _stable_seed(manifest["story_id"], manifest["revision_id"], shot_id, "animation"),
                },
            },
        ]
        speaker = _dialogue_character(shot, screenplay)
        if speaker is not None:
            assets.append({
                "kind": "voice",
                "agent_id": ASSET_AGENTS["voice"],
                "output_extension": provider.output_extension("voice"),
                "request": {
                    "shot_id": shot_id,
                    "scene_id": shot["scene_id"],
                    "duration_seconds": shot["duration_seconds"],
                    "character_id": speaker,
                    "text": str(shot["dialogue"]).strip(),
                    "voice_identity": voices[speaker],
                    "seed": _stable_seed(manifest["story_id"], manifest["revision_id"], shot_id, "voice"),
                },
            })
        assets.append({
            "kind": "sfx",
            "agent_id": ASSET_AGENTS["sfx"],
            "output_extension": provider.output_extension("sfx"),
            "request": {
                "shot_id": shot_id,
                "scene_id": shot["scene_id"],
                "duration_seconds": shot["duration_seconds"],
                "prompt": f"Sound effects only, no speech: {shot['action']}",
                "seed": _stable_seed(manifest["story_id"], manifest["revision_id"], shot_id, "sfx"),
            },
        })
        assets.sort(key=lambda item: ASSET_ORDER.index(item["kind"]))
        plans.append({
            "shot_id": shot_id,
            "scene_id": shot["scene_id"],
            "duration_seconds": shot["duration_seconds"],
            "assets": assets,
        })
    return plans


def validate_asset_plan(payload: dict[str, Any]) -> dict[str, Any]:
    validate_payload(payload, load_schema("asset-plan.schema.json"), label="Asset plan")
    shot_ids = [shot["shot_id"] for shot in payload["shots"]]
    if len(set(shot_ids)) != len(shot_ids):
        raise ProductionGateError("Asset plan memiliki shot_id duplikat.")
    for shot in payload["shots"]:
        kinds = [asset["kind"] for asset in shot["assets"]]
        if len(set(kinds)) != len(kinds):
            raise ProductionGateError(f"Shot {shot['shot_id']} memiliki asset kind duplikat.")
        if not {"reference", "animation", "sfx"}.issubset(kinds):
            raise ProductionGateError(
                f"Shot {shot['shot_id']} wajib memiliki reference, animation, dan sfx."
            )
        for asset in shot["assets"]:
            if asset["agent_id"] != ASSET_AGENTS[asset["kind"]]:
                raise ProductionGateError(
                    f"Agent asset {shot['shot_id']}:{asset['kind']} tidak sesuai kontrak."
                )
    return payload


def prepare_asset_plan(
    manifest_path: str | Path,
    voice_cast_path: str | Path,
    *,
    production_root: str | Path,
    provider_mode: str,
    provider_config_path: str | Path | None = None,
    production_id: str | None = None,
    width: int = 720,
    height: int = 1280,
    fps: float = 30,
) -> Path:
    manifest_path = Path(manifest_path).expanduser().resolve()
    voice_cast_path = Path(voice_cast_path).expanduser().resolve()
    _require_story_approval(manifest_path)
    manifest, context = _story_context(manifest_path)
    voice_cast = validate_voice_cast(
        load_json_object(voice_cast_path, "Voice cast"),
        manifest_path=manifest_path,
    )
    config, config_fingerprint, resolved_config_path = load_provider_config(
        provider_mode,
        provider_config_path,
    )
    provider = build_provider(provider_mode, config)
    if type(width) is not int or type(height) is not int or width < 64 or height < 64:
        raise ProductionGateError("Dimensi produksi harus integer minimal 64 pixel.")
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not 0 < fps <= 240:
        raise ProductionGateError("FPS produksi harus > 0 dan <= 240.")
    production_id = _safe_id(
        production_id or ("prod-" + uuid.uuid4().hex[:12]),
        "production_id",
    )
    production_dir = (
        Path(production_root).expanduser().resolve()
        / safe_story_id(manifest["story_id"])
        / safe_story_id(manifest["revision_id"], label="revision_id")
        / production_id
    )
    plan_path = production_dir / "asset-plan.json"
    if plan_path.exists():
        raise ProductionError(f"Asset plan sudah ada dan tidak akan ditimpa: {plan_path}")
    payload = {
        "artifact_type": "asset-plan",
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "producer_version": PRODUCER_VERSION,
        "story_id": manifest["story_id"],
        "revision_id": manifest["revision_id"],
        "production_id": production_id,
        "story_manifest_path": str(manifest_path),
        "story_manifest_fingerprint": sha256_file(manifest_path),
        "voice_cast_path": str(voice_cast_path),
        "voice_cast_fingerprint": sha256_file(voice_cast_path),
        "provider_mode": provider_mode,
        "provider_config_path": str(resolved_config_path) if resolved_config_path else None,
        "provider_config_fingerprint": config_fingerprint,
        "created_at": utc_now(),
        "video": {"width": width, "height": height, "fps": float(fps)},
        "shots": _asset_requests(
            manifest,
            context,
            voice_cast,
            provider,
            width=width,
            height=height,
            fps=float(fps),
        ),
    }
    validate_asset_plan(payload)
    _atomic_write_json(plan_path, payload)
    return plan_path


def load_asset_plan(path: str | Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    plan_path = Path(path).expanduser().resolve()
    plan = validate_asset_plan(load_json_object(plan_path, "Asset plan"))
    manifest_path = Path(plan["story_manifest_path"]).expanduser().resolve()
    _require_story_approval(manifest_path)
    if sha256_file(manifest_path) != plan["story_manifest_fingerprint"]:
        raise ProductionGateError("Story manifest berubah setelah asset plan dibuat.")
    cast_path = Path(plan["voice_cast_path"]).expanduser().resolve()
    if not cast_path.is_file() or sha256_file(cast_path) != plan["voice_cast_fingerprint"]:
        raise ProductionGateError("Voice cast berubah setelah asset plan dibuat.")
    voice_cast = validate_voice_cast(
        load_json_object(cast_path, "Voice cast"),
        manifest_path=manifest_path,
    )
    config, fingerprint, _ = load_provider_config(
        plan["provider_mode"],
        plan["provider_config_path"],
    )
    if fingerprint != plan["provider_config_fingerprint"]:
        raise ProductionGateError("Provider config berubah setelah asset plan dibuat.")
    manifest, context = _story_context(manifest_path)
    provider = build_provider(plan["provider_mode"], config)
    expected_shots = _asset_requests(
        manifest,
        context,
        voice_cast,
        provider,
        width=plan["video"]["width"],
        height=plan["video"]["height"],
        fps=plan["video"]["fps"],
    )
    if plan["shots"] != expected_shots:
        raise ProductionGateError("Asset plan tidak cocok dengan story, cast, atau provider config.")
    return plan_path, plan, config


def _initial_manifest(plan_path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    timestamp = utc_now()
    return {
        "artifact_type": "asset-manifest",
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "producer_version": PRODUCER_VERSION,
        "story_id": plan["story_id"],
        "revision_id": plan["revision_id"],
        "production_id": plan["production_id"],
        "plan_fingerprint": sha256_file(plan_path),
        "created_at": timestamp,
        "updated_at": timestamp,
        "status": "PENDING",
        "shots": [
            {
                "shot_id": shot["shot_id"],
                "assets": {
                    asset["kind"]: {
                        "state": "PENDING",
                        "active_attempt": None,
                        "attempts": [],
                    }
                    for asset in shot["assets"]
                },
            }
            for shot in plan["shots"]
        ],
    }


def _manifest_status(manifest: dict[str, Any]) -> str:
    states = [
        asset["state"]
        for shot in manifest["shots"]
        for asset in shot["assets"].values()
    ]
    if states and all(state == "COMPLETED" for state in states):
        return "COMPLETED"
    if any(state == "FAILED" for state in states):
        return "FAILED"
    if any(state in {"COMPLETED", "STALE"} for state in states):
        return "PARTIAL"
    return "PENDING"


def _validate_manifest_shape(
    manifest: dict[str, Any],
    *,
    plan_path: Path,
    plan: dict[str, Any],
) -> dict[str, Any]:
    validate_payload(
        manifest,
        load_schema("asset-manifest.schema.json"),
        label="Asset manifest",
    )
    for field in ("story_id", "revision_id", "production_id"):
        if manifest[field] != plan[field]:
            raise ProductionGateError(f"Asset manifest {field} tidak cocok dengan plan.")
    if manifest["plan_fingerprint"] != sha256_file(plan_path):
        raise ProductionGateError("Asset plan berubah setelah manifest dibuat.")
    plan_shots = {shot["shot_id"]: shot for shot in plan["shots"]}
    manifest_shots = {shot["shot_id"]: shot for shot in manifest["shots"]}
    if len(plan_shots) != len(plan["shots"]) or len(manifest_shots) != len(manifest["shots"]):
        raise ProductionGateError("Asset manifest memiliki shot ID duplikat.")
    if set(plan_shots) != set(manifest_shots):
        raise ProductionGateError("Daftar shot asset manifest tidak cocok dengan plan.")
    for shot_id, planned in plan_shots.items():
        expected = {item["kind"] for item in planned["assets"]}
        if set(manifest_shots[shot_id]["assets"]) != expected:
            raise ProductionGateError(f"Daftar asset shot {shot_id} tidak cocok dengan plan.")
    if manifest["status"] != _manifest_status(manifest):
        raise ProductionGateError("Status asset manifest tidak sesuai state asset.")
    return manifest


AssetCallback = Callable[[str, str, str, int, int, bool], None]


@dataclass(frozen=True)
class AssetRunResult:
    manifest_path: Path
    manifest: dict[str, Any]
    generated: int
    reused: int
    failures: tuple[str, ...]


class AssetPipeline:
    def __init__(self, plan_path: str | Path):
        self.plan_path, self.plan, config = load_asset_plan(plan_path)
        self.base = self.plan_path.parent
        self.provider = build_provider(self.plan["provider_mode"], config)
        self.manifest_path = self.base / "asset-manifest.json"

    def _manifest(self) -> dict[str, Any]:
        if self.manifest_path.exists():
            return _validate_manifest_shape(
                load_json_object(self.manifest_path, "Asset manifest"),
                plan_path=self.plan_path,
                plan=self.plan,
            )
        manifest = _initial_manifest(self.plan_path, self.plan)
        validate_payload(
            manifest,
            load_schema("asset-manifest.schema.json"),
            label="Asset manifest",
        )
        _atomic_write_json(self.manifest_path, manifest)
        return manifest

    @staticmethod
    def _shot_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {shot["shot_id"]: shot for shot in manifest["shots"]}

    def _attempt(
        self,
        entry: dict[str, Any],
        *,
        shot_id: str,
        kind: str,
        require_output: bool = True,
    ) -> tuple[Path, dict[str, Any]]:
        relative = entry.get("active_attempt")
        if not relative:
            raise ProductionGateError("Asset completed tidak memiliki active attempt.")
        path = _resolved_child(self.base, relative, "Active attempt")
        records = [item for item in entry["attempts"] if item["path"] == relative]
        if len(records) != 1 or records[0]["state"] != "COMPLETED":
            raise ProductionGateError("Active attempt tidak merujuk tepat satu completed record.")
        if not path.is_file() or sha256_file(path) != records[0]["fingerprint"]:
            raise ProductionGateError("Active attempt berubah atau hilang.")
        attempt = load_json_object(path, "Asset attempt")
        validate_payload(attempt, load_schema("asset-attempt.schema.json"), label="Asset attempt")
        if (
            attempt["story_id"] != self.plan["story_id"]
            or attempt["revision_id"] != self.plan["revision_id"]
            or attempt["production_id"] != self.plan["production_id"]
            or attempt["shot_id"] != shot_id
            or attempt["kind"] != kind
        ):
            raise ProductionGateError(f"Active attempt identity tidak cocok: {shot_id}:{kind}")
        request_path = _resolved_child(self.base, attempt["request_path"], "Asset request")
        if (
            not request_path.is_file()
            or sha256_file(request_path) != attempt["request_fingerprint"]
        ):
            raise ProductionGateError(f"Asset request berubah atau hilang: {shot_id}:{kind}")
        if require_output:
            output = attempt.get("output")
            if attempt["state"] != "COMPLETED" or not isinstance(output, dict):
                raise ProductionGateError("Active attempt bukan output completed.")
            output_path = _resolved_child(self.base, output["path"], "Asset output")
            if (
                not output_path.is_file()
                or output_path.stat().st_size != output["size_bytes"]
                or sha256_file(output_path) != output["fingerprint"]
            ):
                raise ProductionGateError(f"Output asset berubah atau hilang: {output_path}")
        return path, attempt

    def _dependencies(
        self,
        shot_manifest: dict[str, Any],
        kind: str,
    ) -> list[str]:
        if kind != "animation":
            return []
        reference = shot_manifest["assets"]["reference"]
        if reference["state"] != "COMPLETED":
            raise ProductionGateError("Animation memerlukan reference shot yang completed.")
        _, attempt = self._attempt(
            reference,
            shot_id=shot_manifest["shot_id"],
            kind="reference",
        )
        return [attempt["output"]["fingerprint"]]

    def _input_fingerprint(
        self,
        asset: dict[str, Any],
        dependency_artifacts: list[str],
        regeneration_key: str | None = None,
    ) -> str:
        return sha256_json({
            "request": asset["request"],
            "provider": self.provider.describe(asset["kind"]),
            "dependency_artifacts": dependency_artifacts,
            "regeneration_key": regeneration_key,
        })

    def _can_reuse(
        self,
        entry: dict[str, Any],
        input_fingerprint: str,
        *,
        shot_id: str,
        kind: str,
    ) -> bool:
        if entry["state"] != "COMPLETED" or not entry["active_attempt"]:
            return False
        _, attempt = self._attempt(entry, shot_id=shot_id, kind=kind)
        return attempt["input_fingerprint"] == input_fingerprint

    def _save_manifest(self, manifest: dict[str, Any]) -> None:
        manifest["updated_at"] = utc_now()
        manifest["status"] = _manifest_status(manifest)
        validate_payload(
            manifest,
            load_schema("asset-manifest.schema.json"),
            label="Asset manifest",
        )
        _atomic_write_json(self.manifest_path, manifest)

    def _record_attempt(
        self,
        manifest: dict[str, Any],
        shot_id: str,
        kind: str,
        attempt_path: Path,
        attempt: dict[str, Any],
    ) -> None:
        entry = self._shot_map(manifest)[shot_id]["assets"][kind]
        record = {
            "attempt_id": attempt["attempt_id"],
            "state": attempt["state"],
            "path": _relative_path(self.base, attempt_path),
            "fingerprint": sha256_file(attempt_path),
        }
        entry["attempts"].append(record)
        entry["state"] = attempt["state"]
        if attempt["state"] == "COMPLETED":
            entry["active_attempt"] = record["path"]
            if kind == "reference":
                animation = self._shot_map(manifest)[shot_id]["assets"]["animation"]
                if animation["active_attempt"]:
                    animation["state"] = "STALE"
        self._save_manifest(manifest)

    def generate(
        self,
        *,
        shot_ids: list[str] | None = None,
        kinds: list[str] | None = None,
        force: bool = False,
        regeneration_key: str | None = None,
        continue_on_error: bool = False,
        on_asset: AssetCallback | None = None,
    ) -> AssetRunResult:
        manifest = self._manifest()
        planned_shots = {shot["shot_id"]: shot for shot in self.plan["shots"]}
        selected_shots = list(dict.fromkeys(shot_ids or planned_shots.keys()))
        unknown_shots = sorted(set(selected_shots) - set(planned_shots))
        if unknown_shots:
            raise ProductionGateError("Shot tidak ada dalam asset plan: " + ", ".join(unknown_shots))
        selected_kinds = list(dict.fromkeys(kinds or ASSET_ORDER))
        unknown_kinds = sorted(set(selected_kinds) - set(ASSET_ORDER))
        if unknown_kinds:
            raise ProductionGateError("Asset kind tidak dikenal: " + ", ".join(unknown_kinds))
        if regeneration_key is not None:
            regeneration_key = _safe_id(regeneration_key, "regeneration_key")
        work = [
            (shot_id, asset)
            for shot_id in selected_shots
            for asset in planned_shots[shot_id]["assets"]
            if asset["kind"] in selected_kinds
        ]
        if not work:
            raise ProductionGateError("Seleksi tidak menghasilkan asset untuk diproses.")
        generated = 0
        reused = 0
        failures: list[str] = []
        shot_manifest_map = self._shot_map(manifest)
        for index, (shot_id, asset) in enumerate(work, start=1):
            kind = asset["kind"]
            entry = shot_manifest_map[shot_id]["assets"][kind]
            try:
                dependencies = self._dependencies(shot_manifest_map[shot_id], kind)
                input_fingerprint = self._input_fingerprint(
                    asset,
                    dependencies,
                    regeneration_key,
                )
                if not force and self._can_reuse(
                    entry,
                    input_fingerprint,
                    shot_id=shot_id,
                    kind=kind,
                ):
                    reused += 1
                    if on_asset:
                        on_asset(shot_id, kind, asset["agent_id"], index, len(work), True)
                    continue
                attempt_id = "attempt-" + uuid.uuid4().hex[:16]
                attempt_dir = self.base / "attempts" / shot_id / kind / attempt_id
                attempt_dir.mkdir(parents=True, exist_ok=False)
                request_path = attempt_dir / "request.json"
                output_path = attempt_dir / ("output" + asset["output_extension"])
                request = {
                    "schema_version": "1.0",
                    "story_id": self.plan["story_id"],
                    "revision_id": self.plan["revision_id"],
                    "production_id": self.plan["production_id"],
                    "shot_id": shot_id,
                    "kind": kind,
                    "input_fingerprint": input_fingerprint,
                    "dependency_artifacts": dependencies,
                    "request": asset["request"],
                }
                _atomic_write_json(request_path, request)
                started = utc_now()
                try:
                    self.provider.generate(kind, request, request_path, output_path)
                    if not output_path.is_file() or output_path.stat().st_size <= 0:
                        raise AssetProviderError(
                            f"Provider tidak menghasilkan output non-kosong untuk {shot_id}:{kind}."
                        )
                    attempt = {
                        "artifact_type": "asset-attempt",
                        "schema_version": CONTRACT_SCHEMA_VERSION,
                        "producer_version": PRODUCER_VERSION,
                        "story_id": self.plan["story_id"],
                        "revision_id": self.plan["revision_id"],
                        "production_id": self.plan["production_id"],
                        "shot_id": shot_id,
                        "kind": kind,
                        "attempt_id": attempt_id,
                        "input_fingerprint": input_fingerprint,
                        "regeneration_key": regeneration_key,
                        "dependency_artifacts": dependencies,
                        "provider": self.provider.describe(kind),
                        "state": "COMPLETED",
                        "created_at": started,
                        "finished_at": utc_now(),
                        "request_path": _relative_path(self.base, request_path),
                        "request_fingerprint": sha256_file(request_path),
                        "output": {
                            "path": _relative_path(self.base, output_path),
                            "fingerprint": sha256_file(output_path),
                            "size_bytes": output_path.stat().st_size,
                        },
                        "error": None,
                    }
                except Exception as exc:
                    attempt = {
                        "artifact_type": "asset-attempt",
                        "schema_version": CONTRACT_SCHEMA_VERSION,
                        "producer_version": PRODUCER_VERSION,
                        "story_id": self.plan["story_id"],
                        "revision_id": self.plan["revision_id"],
                        "production_id": self.plan["production_id"],
                        "shot_id": shot_id,
                        "kind": kind,
                        "attempt_id": attempt_id,
                        "input_fingerprint": input_fingerprint,
                        "regeneration_key": regeneration_key,
                        "dependency_artifacts": dependencies,
                        "provider": self.provider.describe(kind),
                        "state": "FAILED",
                        "created_at": started,
                        "finished_at": utc_now(),
                        "request_path": _relative_path(self.base, request_path),
                        "request_fingerprint": sha256_file(request_path),
                        "output": None,
                        "error": _clean_error(exc) or type(exc).__name__,
                    }
                validate_payload(
                    attempt,
                    load_schema("asset-attempt.schema.json"),
                    label="Asset attempt",
                )
                attempt_path = attempt_dir / "attempt.json"
                _atomic_write_json(attempt_path, attempt)
                self._record_attempt(manifest, shot_id, kind, attempt_path, attempt)
                if attempt["state"] == "FAILED":
                    failures.append(f"{shot_id}:{kind}")
                else:
                    generated += 1
                if on_asset:
                    on_asset(shot_id, kind, asset["agent_id"], index, len(work), False)
                if attempt["state"] == "FAILED" and not continue_on_error:
                    raise AssetProviderError(
                        f"Asset gagal: {shot_id}:{kind} ({attempt['error']})"
                    )
            except ProductionError:
                if continue_on_error:
                    failure_key = f"{shot_id}:{kind}"
                    if failure_key not in failures:
                        failures.append(failure_key)
                    if on_asset:
                        on_asset(shot_id, kind, asset["agent_id"], index, len(work), False)
                    continue
                raise
        return AssetRunResult(
            manifest_path=self.manifest_path,
            manifest=manifest,
            generated=generated,
            reused=reused,
            failures=tuple(failures),
        )


def validate_asset_production(
    plan_path: str | Path,
    *,
    require_complete: bool = False,
) -> dict[str, Any]:
    pipeline = AssetPipeline(plan_path)
    if not pipeline.manifest_path.is_file():
        raise ProductionGateError(
            f"Asset manifest belum dibuat: {pipeline.manifest_path}"
        )
    manifest = load_json_object(pipeline.manifest_path, "Asset manifest")
    manifest = _validate_manifest_shape(
        manifest,
        plan_path=pipeline.plan_path,
        plan=pipeline.plan,
    )
    manifest_shots = pipeline._shot_map(manifest)
    planned_shots = {shot["shot_id"]: shot for shot in pipeline.plan["shots"]}
    for shot_id, planned in planned_shots.items():
        for asset in planned["assets"]:
            kind = asset["kind"]
            entry = manifest_shots[shot_id]["assets"][kind]
            attempt_paths = [item["path"] for item in entry["attempts"]]
            if len(set(attempt_paths)) != len(attempt_paths):
                raise ProductionGateError(f"Attempt path duplikat: {shot_id}:{kind}")
            for attempt_record in entry["attempts"]:
                attempt_path = _resolved_child(pipeline.base, attempt_record["path"], "Attempt")
                if (
                    not attempt_path.is_file()
                    or sha256_file(attempt_path) != attempt_record["fingerprint"]
                ):
                    raise ProductionGateError(f"Attempt berubah atau hilang: {shot_id}:{kind}")
                attempt = load_json_object(attempt_path, "Asset attempt")
                validate_payload(
                    attempt,
                    load_schema("asset-attempt.schema.json"),
                    label="Asset attempt",
                )
                if (
                    attempt["story_id"] != pipeline.plan["story_id"]
                    or attempt["revision_id"] != pipeline.plan["revision_id"]
                    or attempt["production_id"] != pipeline.plan["production_id"]
                    or attempt["shot_id"] != shot_id
                    or attempt["kind"] != kind
                    or attempt["state"] != attempt_record["state"]
                    or attempt["attempt_id"] != attempt_record["attempt_id"]
                ):
                    raise ProductionGateError(f"Attempt identity tidak cocok: {shot_id}:{kind}")
                request_path = _resolved_child(
                    pipeline.base,
                    attempt["request_path"],
                    "Asset request",
                )
                if (
                    not request_path.is_file()
                    or sha256_file(request_path) != attempt["request_fingerprint"]
                ):
                    raise ProductionGateError(f"Request berubah atau hilang: {shot_id}:{kind}")
                expected_attempt_input = pipeline._input_fingerprint(
                    asset,
                    attempt["dependency_artifacts"],
                    attempt.get("regeneration_key"),
                )
                if attempt["input_fingerprint"] != expected_attempt_input:
                    raise ProductionGateError(f"Input attempt tidak cocok: {shot_id}:{kind}")
                if attempt["state"] == "COMPLETED":
                    output = attempt["output"]
                    output_path = _resolved_child(
                        pipeline.base,
                        output["path"],
                        "Asset output",
                    )
                    if (
                        not output_path.is_file()
                        or output_path.stat().st_size != output["size_bytes"]
                        or sha256_file(output_path) != output["fingerprint"]
                    ):
                        raise ProductionGateError(
                            f"Output attempt berubah atau hilang: {shot_id}:{kind}"
                        )
            if entry["state"] == "COMPLETED":
                dependencies = pipeline._dependencies(manifest_shots[shot_id], kind)
                _, active = pipeline._attempt(entry, shot_id=shot_id, kind=kind)
                expected_input = pipeline._input_fingerprint(
                    asset,
                    dependencies,
                    active.get("regeneration_key"),
                )
                if active["input_fingerprint"] != expected_input:
                    raise ProductionGateError(f"Asset completed sudah stale: {shot_id}:{kind}")
    if require_complete and manifest["status"] != "COMPLETED":
        raise ProductionGateError(
            f"Asset production belum lengkap; status saat ini {manifest['status']}."
        )
    return manifest
