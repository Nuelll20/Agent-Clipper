import json
from pathlib import Path
from jsonschema import validate
from jsonschema.exceptions import ValidationError


def validate_artifact(data_path, schema_path):

    with open(data_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    try:
        validate(
            instance=data,
            schema=schema
        )

        print(
            f"[PASS] {data_path}"
        )

    except ValidationError as error:
        print(
            f"[BLOCK] {data_path}"
        )
        print(error.message)


if __name__ == "__main__":

    print(
        "Hermes Artifact Validator Ready"
    )
