def validate_segments(segments):

    issues = []

    previous_end = 0

    for segment in segments:

        start = segment["start"]
        end = segment["end"]

        if start < previous_end:
            issues.append(
                "TIMING_OVERLAP"
            )

        if len(segment["text"]) > 42:
            issues.append(
                "LINE_TOO_LONG"
            )

        previous_end = end


    return {
        "valid": len(issues) == 0,
        "issues": issues
    }