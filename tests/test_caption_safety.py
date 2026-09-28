from validators.sanitizer import sanitize_transcript
from validators.transcript_validator import validate_transcript


def test_ass_leak():

    raw = "Halo {\\fscx120}"

    cleaned = sanitize_transcript(raw)

    assert "\\fscx" not in cleaned


def test_validator():

    result = validate_transcript(
        "Halo dunia"
    )

    assert result["valid"]


if __name__ == "__main__":

    test_ass_leak()
    test_validator()

    print(
        "Caption safety tests passed"
    )