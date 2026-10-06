"""Versioned story-development artifacts and multi-agent orchestration."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
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
from validators.artifact_validator import (
    ArtifactValidationError,
    load_json_object,
    validate_payload,
)


STORY_STAGES = (
    "universe",
    "outline",
    "screenplay",
    "critique",
    "continuity",
    "shot-list",
)
STORY_AGENTS = {
    "universe": "worldbuilder",
    "outline": "plotter",
    "screenplay": "screenwriter",
    "critique": "story-critic",
    "continuity": "continuity-editor",
    "shot-list": "shot-planner",
}
STORY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class StoryError(RuntimeError):
    """Expected story input, provider, or workspace failure."""


class StoryProviderError(StoryError):
    """The configured generation provider did not return usable JSON."""


class StoryGateError(StoryError):
    """A critique, continuity, lineage, or approval gate blocked progression."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def safe_story_id(value: Any, *, label: str = "story_id") -> str:
    normalized = str(value or "").strip()
    if not STORY_ID_PATTERN.fullmatch(normalized):
        raise ArtifactValidationError(
            f"{label} harus 1-80 karakter: huruf, angka, titik, garis bawah, atau strip."
        )
    return normalized


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
        raise StoryError(f"Artifact cerita tidak dapat ditulis: {path} ({exc})") from None


def _nonempty(value: Any, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ArtifactValidationError(f"{label} wajib diisi.")
    return normalized


def _object_list(value: Any, label: str, *, minimum: int = 1) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) < minimum:
        raise ArtifactValidationError(f"{label} harus berisi minimal {minimum} item.")
    if any(not isinstance(item, dict) for item in value):
        raise ArtifactValidationError(f"Semua item {label} harus berupa object.")
    return value


def _unique_ids(items: list[dict[str, Any]], label: str) -> set[str]:
    identifiers = [_nonempty(item.get("id"), f"{label}[].id") for item in items]
    if len(set(identifiers)) != len(identifiers):
        raise ArtifactValidationError(f"{label} memiliki ID duplikat.")
    return set(identifiers)


def validate_story_brief(payload: dict[str, Any]) -> dict[str, Any]:
    validate_payload(payload, load_schema("story-brief.schema.json"), label="Story brief")
    safe_story_id(payload.get("story_id"))
    return payload


def _validate_universe(content: dict[str, Any]) -> None:
    _nonempty(content.get("premise"), "universe.premise")
    rules = content.get("rules")
    if not isinstance(rules, list) or not rules or any(not str(item).strip() for item in rules):
        raise ArtifactValidationError("universe.rules harus berupa daftar teks non-kosong.")
    characters = _object_list(content.get("characters"), "universe.characters")
    locations = _object_list(content.get("locations"), "universe.locations")
    _unique_ids(characters, "universe.characters")
    _unique_ids(locations, "universe.locations")
    for item in characters:
        for field in ("name", "role", "goal", "visual_identity"):
            _nonempty(item.get(field), f"character.{field}")
        traits = item.get("traits")
        if not isinstance(traits, list) or not traits:
            raise ArtifactValidationError("character.traits harus berupa daftar non-kosong.")
    for item in locations:
        _nonempty(item.get("name"), "location.name")
        _nonempty(item.get("description"), "location.description")


def _validate_outline(content: dict[str, Any], universe: dict[str, Any]) -> None:
    for field in ("title", "logline", "theme"):
        _nonempty(content.get(field), f"outline.{field}")
    character_ids = _unique_ids(universe["characters"], "universe.characters")
    location_ids = _unique_ids(universe["locations"], "universe.locations")
    acts = _object_list(content.get("acts"), "outline.acts")
    seen_beats: set[str] = set()
    for act in acts:
        if type(act.get("act")) is not int or act["act"] < 1:
            raise ArtifactValidationError("outline.acts[].act harus integer positif.")
        _nonempty(act.get("summary"), "outline.acts[].summary")
        for beat in _object_list(act.get("beats"), "outline.acts[].beats"):
            beat_id = _nonempty(beat.get("id"), "outline beat id")
            if beat_id in seen_beats:
                raise ArtifactValidationError("outline memiliki beat ID duplikat.")
            seen_beats.add(beat_id)
            _nonempty(beat.get("summary"), f"outline beat {beat_id}.summary")
            if beat.get("location_id") not in location_ids:
                raise ArtifactValidationError(
                    f"Outline beat {beat_id} merujuk location_id yang tidak dikenal."
                )
            characters = beat.get("characters")
            if not isinstance(characters, list) or any(item not in character_ids for item in characters):
                raise ArtifactValidationError(
                    f"Outline beat {beat_id} merujuk character ID yang tidak dikenal."
                )


def _validate_screenplay(content: dict[str, Any], universe: dict[str, Any]) -> None:
    _nonempty(content.get("title"), "screenplay.title")
    character_ids = _unique_ids(universe["characters"], "universe.characters")
    location_ids = _unique_ids(universe["locations"], "universe.locations")
    scenes = _object_list(content.get("scenes"), "screenplay.scenes")
    _unique_ids(scenes, "screenplay.scenes")
    for scene in scenes:
        scene_id = str(scene["id"])
        for field in ("heading", "time_of_day", "summary"):
            _nonempty(scene.get(field), f"scene {scene_id}.{field}")
        if scene.get("location_id") not in location_ids:
            raise ArtifactValidationError(
                f"Scene {scene_id} merujuk location_id yang tidak dikenal."
            )
        duration = scene.get("duration_seconds")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
            raise ArtifactValidationError(f"Scene {scene_id} memiliki durasi tidak valid.")
        beats = _object_list(scene.get("beats"), f"scene {scene_id}.beats")
        for beat in beats:
            beat_type = str(beat.get("type") or "").upper()
            if beat_type not in {"ACTION", "DIALOGUE"}:
                raise ArtifactValidationError(f"Scene {scene_id} memiliki beat type tidak valid.")
            _nonempty(beat.get("text"), f"scene {scene_id}.beat.text")
            character = beat.get("character")
            if beat_type == "DIALOGUE" and character not in character_ids:
                raise ArtifactValidationError(
                    f"Scene {scene_id} memiliki pembicara yang tidak dikenal."
                )


def _validate_critique(content: dict[str, Any]) -> None:
    if content.get("verdict") not in {"PASS", "REVISE"}:
        raise ArtifactValidationError("critique.verdict harus PASS atau REVISE.")
    _nonempty(content.get("summary"), "critique.summary")
    issues = content.get("issues")
    if not isinstance(issues, list) or any(not isinstance(item, dict) for item in issues):
        raise ArtifactValidationError("critique.issues harus berupa daftar object.")
    for issue in issues:
        if issue.get("severity") not in {"BLOCKER", "MAJOR", "MINOR"}:
            raise ArtifactValidationError("critique issue severity tidak valid.")
        for field in ("category", "message", "recommendation"):
            _nonempty(issue.get(field), f"critique issue.{field}")
    if content["verdict"] == "PASS" and any(
        issue.get("severity") in {"BLOCKER", "MAJOR"} for issue in issues
    ):
        raise ArtifactValidationError(
            "Critique PASS tidak boleh menyisakan issue BLOCKER atau MAJOR."
        )


def _validate_continuity(content: dict[str, Any]) -> None:
    if content.get("status") not in {"PASS", "BLOCKED"}:
        raise ArtifactValidationError("continuity.status harus PASS atau BLOCKED.")
    checks = _object_list(content.get("checks"), "continuity.checks")
    has_failure = False
    for check in checks:
        if check.get("status") not in {"PASS", "WARN", "FAIL"}:
            raise ArtifactValidationError("continuity check status tidak valid.")
        has_failure = has_failure or check["status"] == "FAIL"
        _nonempty(check.get("category"), "continuity check.category")
        _nonempty(check.get("message"), "continuity check.message")
    if (content["status"] == "BLOCKED") != has_failure:
        raise ArtifactValidationError(
            "continuity.status harus BLOCKED tepat ketika terdapat check FAIL."
        )


def _validate_shot_list(
    content: dict[str, Any],
    universe: dict[str, Any],
    screenplay: dict[str, Any],
) -> None:
    scene_ids = _unique_ids(screenplay["scenes"], "screenplay.scenes")
    character_ids = _unique_ids(universe["characters"], "universe.characters")
    location_ids = _unique_ids(universe["locations"], "universe.locations")
    shots = _object_list(content.get("shots"), "shot-list.shots")
    _unique_ids(shots, "shot-list.shots")
    orders: set[tuple[str, int]] = set()
    for shot in shots:
        shot_id = str(shot["id"])
        if shot.get("scene_id") not in scene_ids:
            raise ArtifactValidationError(f"Shot {shot_id} merujuk scene yang tidak dikenal.")
        if shot.get("location_id") not in location_ids:
            raise ArtifactValidationError(f"Shot {shot_id} merujuk lokasi yang tidak dikenal.")
        order = shot.get("order")
        if type(order) is not int or order < 1:
            raise ArtifactValidationError(f"Shot {shot_id} memiliki order tidak valid.")
        order_key = (str(shot["scene_id"]), order)
        if order_key in orders:
            raise ArtifactValidationError("Shot list memiliki order duplikat dalam satu scene.")
        orders.add(order_key)
        duration = shot.get("duration_seconds")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
            raise ArtifactValidationError(f"Shot {shot_id} memiliki durasi tidak valid.")
        for field in ("framing", "camera", "action", "continuity_notes"):
            _nonempty(shot.get(field), f"shot {shot_id}.{field}")
        characters = shot.get("characters")
        if not isinstance(characters, list) or any(item not in character_ids for item in characters):
            raise ArtifactValidationError(f"Shot {shot_id} merujuk karakter yang tidak dikenal.")


def validate_stage_artifact(
    artifact: dict[str, Any],
    *,
    context: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    validate_payload(
        artifact,
        load_schema("story-stage.schema.json"),
        label="Story stage artifact",
    )
    stage = artifact["stage"]
    expected_type = "story-" + stage
    if artifact["artifact_type"] != expected_type:
        raise ArtifactValidationError(
            f"artifact_type {artifact['artifact_type']!r} tidak cocok dengan stage {stage!r}."
        )
    if artifact["agent_id"] != STORY_AGENTS[stage]:
        raise ArtifactValidationError(f"agent_id untuk stage {stage} tidak sesuai kontrak.")
    content = artifact["content"]
    available = context or {}
    if stage == "universe":
        _validate_universe(content)
    elif stage == "outline":
        _validate_outline(content, available["universe"])
    elif stage == "screenplay":
        _validate_screenplay(content, available["universe"])
    elif stage == "critique":
        _validate_critique(content)
    elif stage == "continuity":
        _validate_continuity(content)
    elif stage == "shot-list":
        _validate_shot_list(content, available["universe"], available["screenplay"])
    return artifact


class StoryProvider(Protocol):
    name: str

    def generate(
        self,
        stage: str,
        *,
        brief: dict[str, Any],
        context: dict[str, dict[str, Any]],
    ) -> dict[str, Any]: ...

    def configuration(self) -> dict[str, Any]: ...


STAGE_INSTRUCTIONS = {
    "universe": (
        "Return {premise, rules:[text], characters:[{id,name,role,goal,traits:[text],"
        "visual_identity}], locations:[{id,name,description}]}."
    ),
    "outline": (
        "Return {title, logline, theme, acts:[{act,summary,beats:[{id,summary,"
        "characters:[character_id],location_id}]}]}. Use only universe IDs."
    ),
    "screenplay": (
        "Return {title, scenes:[{id,heading,location_id,time_of_day,summary,"
        "duration_seconds,beats:[{type:ACTION|DIALOGUE,character?,text}]}]}."
    ),
    "critique": (
        "Return {verdict:PASS|REVISE, summary, issues:[{severity:BLOCKER|MAJOR|MINOR,"
        "category,message,target_id,recommendation}]}. PASS may only contain MINOR issues."
    ),
    "continuity": (
        "Return {status:PASS|BLOCKED, checks:[{category,status:PASS|WARN|FAIL,"
        "message,target_id}]}. BLOCKED exactly when a FAIL exists."
    ),
    "shot-list": (
        "Return {shots:[{id,scene_id,order,duration_seconds,framing,camera,action,"
        "dialogue,characters:[character_id],location_id,continuity_notes}]}."
    ),
}


class OpenAICompatibleStoryProvider:
    """Small stdlib client for a local OpenAI-compatible chat endpoint."""

    name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = 180,
    ):
        parsed = urllib.parse.urlparse(str(base_url).strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise StoryProviderError("Base URL provider harus berupa http(s) URL yang valid.")
        self.base_url = str(base_url).rstrip("/")
        self.model = _nonempty(model, "model")
        self.api_key = str(api_key or "").strip()
        if timeout_seconds <= 0:
            raise StoryProviderError("Provider timeout harus lebih besar dari nol.")
        self.timeout_seconds = float(timeout_seconds)

    def configuration(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "base_url": self.base_url,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
        }

    def _url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return self.base_url + "/chat/completions"

    @staticmethod
    def _decode_content(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        text = str(value or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].strip().lower() in {"```", "```json"}:
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StoryProviderError(f"Provider tidak mengembalikan JSON valid: {exc}") from None
        if not isinstance(decoded, dict):
            raise StoryProviderError("Provider harus mengembalikan JSON object.")
        return decoded

    def generate(
        self,
        stage: str,
        *,
        brief: dict[str, Any],
        context: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        if stage not in STORY_STAGES:
            raise StoryProviderError(f"Stage provider tidak dikenal: {stage}")
        request_payload = {
            "model": self.model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"You are the {STORY_AGENTS[stage]} agent in a local animation studio. "
                        "Return only one JSON object. Never invent IDs that conflict with context."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task": STAGE_INSTRUCTIONS[stage],
                            "brief": brief,
                            "approved_upstream_context": context,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self._url(),
            data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", errors="replace")
            raise StoryProviderError(f"Provider HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise StoryProviderError(f"Provider gagal diakses: {type(exc).__name__}: {exc}") from None
        try:
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise StoryProviderError("Respons provider tidak memiliki choices[0].message.content.") from None
        return self._decode_content(content)


class FixtureStoryProvider:
    """Deterministic offline provider for tests and explicit local smoke runs."""

    name = "fixture"

    def configuration(self) -> dict[str, Any]:
        return {"provider": self.name, "version": 1}

    def generate(
        self,
        stage: str,
        *,
        brief: dict[str, Any],
        context: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        title = brief["title"]
        if stage == "universe":
            return {
                "premise": brief["premise"],
                "rules": ["Perubahan karakter harus terlihat melalui tindakan."],
                "characters": [{
                    "id": "char-protagonist",
                    "name": "Ari",
                    "role": "protagonist",
                    "goal": "Menyelesaikan konflik utama cerita.",
                    "traits": ["tekun", "ingin tahu"],
                    "visual_identity": "Siluet sederhana dengan aksen warna hangat.",
                }],
                "locations": [{
                    "id": "loc-main",
                    "name": "Ruang Utama",
                    "description": "Lokasi utama yang mudah dipertahankan kontinuitasnya.",
                }],
            }
        if stage == "outline":
            return {
                "title": title,
                "logline": brief["premise"],
                "theme": "Ketekunan menghadapi perubahan.",
                "acts": [{
                    "act": 1,
                    "summary": "Tokoh menghadapi masalah dan mengambil keputusan.",
                    "beats": [{
                        "id": "beat-01",
                        "summary": "Ari menemukan masalah utama.",
                        "characters": ["char-protagonist"],
                        "location_id": "loc-main",
                    }],
                }],
            }
        if stage == "screenplay":
            return {
                "title": title,
                "scenes": [{
                    "id": "scene-01",
                    "heading": "INT. RUANG UTAMA - PAGI",
                    "location_id": "loc-main",
                    "time_of_day": "pagi",
                    "summary": "Ari memilih untuk bertindak.",
                    "duration_seconds": brief["target_duration_seconds"],
                    "beats": [
                        {"type": "ACTION", "text": "Ari mengamati keadaan ruangan."},
                        {
                            "type": "DIALOGUE",
                            "character": "char-protagonist",
                            "text": "Aku harus menyelesaikan ini.",
                        },
                    ],
                }],
            }
        if stage == "critique":
            return {"verdict": "PASS", "summary": "Struktur layak dilanjutkan.", "issues": []}
        if stage == "continuity":
            return {
                "status": "PASS",
                "checks": [{
                    "category": "character",
                    "status": "PASS",
                    "message": "Identitas karakter konsisten.",
                    "target_id": "char-protagonist",
                }],
            }
        if stage == "shot-list":
            return {
                "shots": [{
                    "id": "shot-01",
                    "scene_id": "scene-01",
                    "order": 1,
                    "duration_seconds": brief["target_duration_seconds"],
                    "framing": "medium shot",
                    "camera": "static with subtle push-in",
                    "action": "Ari mengamati ruangan lalu menghadap kamera.",
                    "dialogue": "Aku harus menyelesaikan ini.",
                    "characters": ["char-protagonist"],
                    "location_id": "loc-main",
                    "continuity_notes": "Pertahankan aksen warna hangat dan arah pandang.",
                }],
            }
        raise StoryProviderError(f"Stage fixture tidak dikenal: {stage}")


StageCallback = Callable[[str, str, int, int, bool], None]


@dataclass(frozen=True)
class StoryRunResult:
    story_id: str
    revision_id: str
    revision_dir: Path
    manifest_path: Path
    manifest: dict[str, Any]


class StoryPipeline:
    """Runs the story roles sequentially with immutable, resumable artifacts."""

    def __init__(self, output_root: str | Path, provider: StoryProvider):
        self.output_root = Path(output_root).expanduser().resolve()
        self.provider = provider

    def _revision_dir(self, story_id: str, revision_id: str) -> Path:
        return self.output_root / safe_story_id(story_id) / "revisions" / safe_story_id(
            revision_id,
            label="revision_id",
        )

    def _build_artifact(
        self,
        *,
        stage: str,
        story_id: str,
        revision_id: str,
        brief_fingerprint: str,
        config_fingerprint: str,
        upstream_fingerprints: list[str],
        content: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "artifact_type": "story-" + stage,
            "schema_version": CONTRACT_SCHEMA_VERSION,
            "producer_version": PRODUCER_VERSION,
            "story_id": story_id,
            "revision_id": revision_id,
            "stage": stage,
            "agent_id": STORY_AGENTS[stage],
            "source_fingerprint": brief_fingerprint,
            "config_fingerprint": config_fingerprint,
            "created_at": utc_now(),
            "upstream_artifacts": upstream_fingerprints,
            "provider": self.provider.configuration(),
            "content": content,
        }

    def _reuse_or_generate(
        self,
        path: Path,
        *,
        stage: str,
        story_id: str,
        revision_id: str,
        brief: dict[str, Any],
        brief_fingerprint: str,
        config_fingerprint: str,
        upstream_fingerprints: list[str],
        context: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        if path.exists():
            existing = load_json_object(path, f"Story stage {stage}")
            validate_stage_artifact(existing, context=context)
            expected = {
                "story_id": story_id,
                "revision_id": revision_id,
                "stage": stage,
                "source_fingerprint": brief_fingerprint,
                "config_fingerprint": config_fingerprint,
                "upstream_artifacts": upstream_fingerprints,
            }
            if any(existing.get(key) != value for key, value in expected.items()):
                raise StoryGateError(
                    f"Artifact {stage} existing tidak cocok dengan input revisi; "
                    "gunakan revision_id baru."
                )
            return existing, True
        content = self.provider.generate(stage, brief=brief, context=context)
        if not isinstance(content, dict):
            raise StoryProviderError(f"Agent {STORY_AGENTS[stage]} tidak menghasilkan object.")
        artifact = self._build_artifact(
            stage=stage,
            story_id=story_id,
            revision_id=revision_id,
            brief_fingerprint=brief_fingerprint,
            config_fingerprint=config_fingerprint,
            upstream_fingerprints=upstream_fingerprints,
            content=content,
        )
        validate_stage_artifact(artifact, context=context)
        _atomic_write_json(path, artifact)
        return artifact, False

    def run(
        self,
        brief_path: str | Path,
        *,
        revision_id: str | None = None,
        on_stage: StageCallback | None = None,
    ) -> StoryRunResult:
        source = Path(brief_path).expanduser().resolve()
        brief = validate_story_brief(load_json_object(source, "Story brief"))
        story_id = safe_story_id(brief["story_id"])
        revision_id = safe_story_id(
            revision_id or ("rev-" + uuid.uuid4().hex[:12]),
            label="revision_id",
        )
        revision_dir = self._revision_dir(story_id, revision_id)
        revision_dir.mkdir(parents=True, exist_ok=True)
        brief_fingerprint = sha256_file(source)
        config_fingerprint = sha256_json(self.provider.configuration())
        context: dict[str, dict[str, Any]] = {}
        upstream_fingerprints: list[str] = []
        entries: list[dict[str, Any]] = []

        for index, stage in enumerate(STORY_STAGES, start=1):
            path = revision_dir / f"{stage}.json"
            artifact, reused = self._reuse_or_generate(
                path,
                stage=stage,
                story_id=story_id,
                revision_id=revision_id,
                brief=brief,
                brief_fingerprint=brief_fingerprint,
                config_fingerprint=config_fingerprint,
                upstream_fingerprints=list(upstream_fingerprints),
                context=context,
            )
            fingerprint = sha256_file(path)
            context[stage] = artifact["content"]
            upstream_fingerprints.append(fingerprint)
            entries.append({
                "stage": stage,
                "agent_id": STORY_AGENTS[stage],
                "path": path.name,
                "fingerprint": fingerprint,
            })
            if on_stage is not None:
                on_stage(stage, STORY_AGENTS[stage], index, len(STORY_STAGES), reused)
            if stage == "critique" and artifact["content"]["verdict"] != "PASS":
                raise StoryGateError(
                    "Story critic meminta revisi; continuity dan shot list tidak dijalankan."
                )
            if stage == "continuity" and artifact["content"]["status"] != "PASS":
                raise StoryGateError(
                    "Continuity check diblokir; shot list tidak dijalankan."
                )

        manifest = {
            "artifact_type": "story-manifest",
            "schema_version": CONTRACT_SCHEMA_VERSION,
            "producer_version": PRODUCER_VERSION,
            "story_id": story_id,
            "revision_id": revision_id,
            "source_fingerprint": brief_fingerprint,
            "config_fingerprint": config_fingerprint,
            "created_at": utc_now(),
            "status": "REVIEW_REQUIRED",
            "artifacts": entries,
        }
        validate_payload(
            manifest,
            load_schema("story-manifest.schema.json"),
            label="Story manifest",
        )
        manifest_path = revision_dir / "story-manifest.json"
        if manifest_path.exists():
            existing_manifest = load_json_object(manifest_path, "Story manifest")
            validate_payload(
                existing_manifest,
                load_schema("story-manifest.schema.json"),
                label="Story manifest",
            )
            comparable = {key: value for key, value in manifest.items() if key != "created_at"}
            existing_comparable = {
                key: value for key, value in existing_manifest.items() if key != "created_at"
            }
            if existing_comparable != comparable:
                raise StoryGateError(
                    "Manifest existing berbeda dari artifact revisi; gunakan revision_id baru."
                )
            manifest = existing_manifest
        else:
            _atomic_write_json(manifest_path, manifest)
        return StoryRunResult(
            story_id=story_id,
            revision_id=revision_id,
            revision_dir=revision_dir,
            manifest_path=manifest_path,
            manifest=manifest,
        )


def write_story_brief(path: str | Path, payload: dict[str, Any]) -> Path:
    """Validate and atomically create a story brief without overwriting one."""
    target = Path(path).expanduser().resolve()
    validate_story_brief(payload)
    if target.exists():
        raise StoryError(f"Story brief sudah ada dan tidak akan ditimpa: {target}")
    _atomic_write_json(target, payload)
    return target


def validate_story_revision(manifest_path: str | Path) -> dict[str, Any]:
    path = Path(manifest_path).expanduser().resolve()
    manifest = load_json_object(path, "Story manifest")
    validate_payload(
        manifest,
        load_schema("story-manifest.schema.json"),
        label="Story manifest",
    )
    context: dict[str, dict[str, Any]] = {}
    observed_stages: list[str] = []
    observed_fingerprints: list[str] = []
    for entry in manifest["artifacts"]:
        stage = entry["stage"]
        artifact_path = path.parent / entry["path"]
        if not artifact_path.is_file() or artifact_path.parent != path.parent:
            raise StoryGateError(f"Artifact stage tidak ditemukan dalam revision dir: {stage}")
        actual = sha256_file(artifact_path)
        if actual != entry["fingerprint"]:
            raise StoryGateError(f"Fingerprint artifact {stage} berubah setelah manifest dibuat.")
        artifact = load_json_object(artifact_path, f"Story stage {stage}")
        validate_stage_artifact(artifact, context=context)
        if artifact["story_id"] != manifest["story_id"] or artifact["revision_id"] != manifest["revision_id"]:
            raise StoryGateError(f"Identitas artifact {stage} tidak cocok dengan manifest.")
        if artifact["source_fingerprint"] != manifest["source_fingerprint"]:
            raise StoryGateError(f"Source lineage artifact {stage} tidak cocok.")
        if artifact["config_fingerprint"] != manifest["config_fingerprint"]:
            raise StoryGateError(f"Config lineage artifact {stage} tidak cocok.")
        if artifact["upstream_artifacts"] != observed_fingerprints:
            raise StoryGateError(f"Upstream lineage artifact {stage} tidak cocok.")
        observed_stages.append(stage)
        observed_fingerprints.append(actual)
        context[stage] = artifact["content"]
    if observed_stages != list(STORY_STAGES):
        raise StoryGateError("Urutan stage pada story manifest tidak lengkap atau berubah.")
    if context["critique"]["verdict"] != "PASS":
        raise StoryGateError("Story revision belum lolos critique.")
    if context["continuity"]["status"] != "PASS":
        raise StoryGateError("Story revision belum lolos continuity.")
    return manifest


def review_story(
    manifest_path: str | Path,
    *,
    decision: str,
    reviewer: str,
    note: str = "",
) -> tuple[dict[str, Any], Path]:
    path = Path(manifest_path).expanduser().resolve()
    manifest = validate_story_revision(path)
    normalized_decision = str(decision or "").strip().upper().replace("-", "_")
    if normalized_decision not in {"APPROVED", "CHANGES_REQUESTED"}:
        raise StoryGateError("Decision harus APPROVED atau CHANGES_REQUESTED.")
    reviewer = _nonempty(reviewer, "reviewer")
    if normalized_decision == "CHANGES_REQUESTED":
        _nonempty(note, "note untuk changes requested")
    review = {
        "artifact_type": "story-review",
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "producer_version": PRODUCER_VERSION,
        "story_id": manifest["story_id"],
        "revision_id": manifest["revision_id"],
        "manifest_fingerprint": sha256_file(path),
        "decision": normalized_decision,
        "reviewer": reviewer,
        "note": str(note or "").strip(),
        "created_at": utc_now(),
    }
    validate_payload(review, load_schema("story-review.schema.json"), label="Story review")
    reviews_dir = path.parent / "reviews"
    review_path = reviews_dir / (
        dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        + "-"
        + uuid.uuid4().hex[:8]
        + ".json"
    )
    _atomic_write_json(review_path, review)
    _atomic_write_json(path.parent / "review.json", review)
    return review, review_path


def assert_story_approved(manifest_path: str | Path) -> dict[str, Any]:
    path = Path(manifest_path).expanduser().resolve()
    manifest = validate_story_revision(path)
    review_path = path.parent / "review.json"
    review = load_json_object(review_path, "Latest story review")
    validate_payload(review, load_schema("story-review.schema.json"), label="Story review")
    if review["story_id"] != manifest["story_id"] or review["revision_id"] != manifest["revision_id"]:
        raise StoryGateError("Review tidak ditujukan untuk story revision ini.")
    if review["manifest_fingerprint"] != sha256_file(path):
        raise StoryGateError("Story manifest berubah setelah review dibuat.")
    if review["decision"] != "APPROVED":
        raise StoryGateError("Story revision belum disetujui untuk produksi.")
    return review
