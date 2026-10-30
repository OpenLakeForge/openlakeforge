"""AWS credential_process entry point used only by Terraform's scoped profile."""

from __future__ import annotations

import json
import os

from olf.auth import aws_process_credentials


def main() -> None:
    print(json.dumps(aws_process_credentials(os.environ)))  # noqa: T201 - credential_process protocol requires stdout JSON.


if __name__ == "__main__":
    main()
