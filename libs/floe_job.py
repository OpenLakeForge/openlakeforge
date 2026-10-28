"""Entrypoint of a domain's Floe Kubernetes Job (`olf-floe-job`).

dagster-floe starts the Job as `olf-floe-job run -c <project> -p <profile>
... --entities <a,b>`, naming the profile path the run pod rendered. That
file lives in the run pod, so it is rendered again here, at the same path and
from the same forwarded contract values, before handing the arguments to
`floe` unchanged. Nothing is printed: dagster-floe reads Floe's JSON events
from this container's log.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from libs.floe_profile import write_profile


def _profile_argument(args: list[str]) -> Path | None:
    for flag in ("-p", "--profile"):
        if flag in args[:-1]:
            return Path(args[args.index(flag) + 1])
    return None


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    target = _profile_argument(args)
    if target is not None:
        write_profile(target=target, in_job=True)
    os.execvp("floe", ["floe", *args])


if __name__ == "__main__":
    main()
