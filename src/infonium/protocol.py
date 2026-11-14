import json
import sys

from infonium.api import app


def main() -> None:
    json.dump(app.openapi(), sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")
