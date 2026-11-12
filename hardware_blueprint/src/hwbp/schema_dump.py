"""Print the project-document JSON Schema: ``python -m hwbp.schema_dump > schema/project.schema.json``."""

import json
import sys

from hwbp.models import project_schema

if __name__ == "__main__":  # pragma: no cover
    sys.stdout.write(json.dumps(project_schema(), indent=2) + "\n")
