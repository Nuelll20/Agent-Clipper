from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


def default_font_dirs() -> list[Path]:
    if os.name == "nt":
        windows_dir = Path(os.environ.get("WINDIR", r"C:\Windows"))
        return [windows_dir / "Fonts"]

    return [
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path.home() / ".fonts",
    ]


def validate_font_manifest(
    manifest_path: Path,
    font_dirs: list[Path] | None = None,
) -> dict[str, Any]:
    manifest = json.loads(
        manifest_path.read_text(encoding="utf-8-sig")
    )

    fonts = manifest.get("fonts")
    issues: list[str] = []
    resolved: list[dict[str, str]] = []

    if not isinstance(fonts, list) or not fonts:
        return {
            "valid": False,
            "issues": ["FONT_MANIFEST_EMPTY"],
            "resolved": [],
        }

    directories = (
        default_font_dirs()
        if font_dirs is None
        else [Path(item) for item in font_dirs]
    )

    available: dict[str, Path] = {}

    for directory in directories:
        if not directory.is_dir():
            continue

        for candidate in directory.rglob("*"):
            if candidate.is_file():
                available.setdefault(
                    candidate.name.casefold(),
                    candidate,
                )

    for font in fonts:
        family = str(font.get("family") or "").strip()
        required = bool(font.get("required", True))
        candidate_files = font.get("candidate_files") or []

        if not family:
            issues.append("FONT_FAMILY_MISSING")
            continue

        match = next(
            (
                available[name.casefold()]
                for name in candidate_files
                if name.casefold() in available
            ),
            None,
        )

        if match is not None:
            resolved.append({
                "family": family,
                "path": str(match),
            })
        elif required:
            issues.append(f"REQUIRED_FONT_MISSING:{family}")

    return {
        "valid": len(issues) == 0,
        "issues": issues,
        "resolved": resolved,
    }


def assert_required_fonts(manifest_path: Path) -> dict[str, Any]:
    result = validate_font_manifest(manifest_path)

    if not result["valid"]:
        raise RuntimeError(
            "FONT_PREFLIGHT_FAILED: "
            + ", ".join(result["issues"])
        )

    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()

    result = validate_font_manifest(args.manifest.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))

    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
