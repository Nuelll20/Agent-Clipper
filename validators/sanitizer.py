import re


ASS_PATTERN = re.compile(
    r"\{.*?(\\pos|\\fscx|\\fscy|\\t|\\c&H).*?\}",
    re.DOTALL
)


def sanitize_transcript(text: str) -> str:
    """
    Remove unsafe ASS override tags
    before subtitle generation.
    """

    cleaned = ASS_PATTERN.sub("", text)

    cleaned = (
        cleaned
        .replace("\x00", "")
        .replace("\x1b", "")
    )

    return cleaned.strip()


def detect_ass_leak(text: str) -> bool:
    return bool(ASS_PATTERN.search(text))