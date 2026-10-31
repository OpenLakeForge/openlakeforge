"""Operations that change state on a person's behalf, each run inside `audit.audited` (ADR 0021, decision 3)."""

from __future__ import annotations

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from openlakeforge_platform_api import reads
from openlakeforge_platform_api.audit import audited
from openlakeforge_platform_api.tokens import Principal

# Tags on every run the API launches: who asked for it, and the request it came from, by which a launch whose
# response was lost is found rather than launched twice.
LAUNCHED_BY_TAG = "openlakeforge/launched-by"
LAUNCH_KEY_TAG = "openlakeforge/launch-key"


class LaunchRequest(BaseModel):
    # A Dagster job name is a Python identifier; anything else cannot name one.
    job: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


def launch_run(request: Request, stage: str, body: LaunchRequest) -> dict[str, str]:
    """Launch one of the stage's jobs as the signed-in person; the run is tagged with who asked for it."""
    principal: Principal = request.state.principal
    request_id: str = request.state.request_id
    with audited(request, stage=stage, target={"job": body.job}) as outcome:
        client = reads._dagster(request, stage)
        with reads._dagster_errors(stage):
            # The job is found among the jobs the read endpoint lists, which neither waits nor reloads a code
            # location as the e2e's launch does: an unknown name is answered at once.
            job = next((job for job in client.list_jobs() if job.name == body.job), None)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no job {body.job!r} in stage {stage!r}")
            run_id = client.launch(
                job.name,
                tags={LAUNCHED_BY_TAG: principal.email or principal.subject},
                repository=(job.location, job.repository),
                launch_key=request_id,
                key_tag=LAUNCH_KEY_TAG,
            )
        outcome.result["run_id"] = run_id
    return {"run_id": run_id, "job": job.name, "stage": stage, "request_id": request_id}
