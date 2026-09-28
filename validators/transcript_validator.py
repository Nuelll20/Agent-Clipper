from validators.sanitizer import detect_ass_leak


def validate_transcript(text):

    issues = []

    if detect_ass_leak(text):
        issues.append(
            "RAW_ASS_TAG_DETECTED"
        )

    if len(text.strip()) == 0:
        issues.append(
            "EMPTY_TRANSCRIPT"
        )

    return {
        "valid": len(issues) == 0,
        "issues": issues
    }