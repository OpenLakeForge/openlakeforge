"""The quality writer: one OpenMetadata bot per governed stage that may publish quality results and nothing else.

The quality reconciler and `olf infra openmetadata quality-sync` publish with it (ADR 0007, Identity). Each stage's
bot has its own token in its own Secret, so a stage's credential is revoked alone and OpenMetadata records which
stage's pass wrote a result. The stage boundary itself is still the pass's configuration and ops-bucket key:
OpenMetadata 1.13.6 policies have no condition on a table's FQN, so the role cannot tell one stage's tables from
another's.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from openlakeforge_services.base import ServiceClientError
from openlakeforge_services.openmetadata import OpenMetadataClient, OpenMetadataError

from olf.common import log
from olf.infra import k8s
from olf.infra.access import Route
from olf.infra.openmetadata._bootstrap import _JSON, _bearer_token, _token_works
from olf.infra.openmetadata._stage_services import STAGES

POLICY = "OlfQualityWriterPolicy"
ROLE = "OlfQualityWriterRole"
JWT_KEY = "OPENMETADATA_QUALITY_WRITER_JWT"
_UPSERT = (200, 201)

# Exactly what the publisher's calls (openlakeforge_services.quality_publisher.QualityCatalog) are authorized
# against on 1.13.6:
# - GET tables/name/{fqn}?fields=columns: VIEW_BASIC on the table (TableResource maps `columns` to it).
# - PUT testDefinitions: CREATE on the definition, or EDIT_ALL once it exists (EntityResource.createOrUpdate).
# - PUT testCases: EDIT_TESTS on the table, or CREATE or EDIT_ALL on the case, any one of them.
# - POST testCaseResults/{fqn}: EDIT_TESTS on the table or EDIT_ALL on the case; GET: VIEW_TESTS on the table or
#   VIEW_ALL on the case. There is no testCaseResult resource to grant.
# The case-side alternatives cannot create a case: DefaultAuthorizer tries them with a context that resolves the case
# by name, and a case that does not exist yet raises EntityNotFoundException, which the ANY evaluation does not catch.
# EDIT_TESTS on the table is therefore the one grant that works, and it covers the results too. VIEW_TESTS is chosen
# over VIEW_ALL on the case, which would also match ViewTestCaseFailedRowsSample, the failed rows a case may hold.
# The table's test suite is created by the server with the first case, so no testSuite grant is needed.
RULES: tuple[dict[str, Any], ...] = (
    {
        "name": "QualityWriter-Tables",
        "description": "Find a registered table and its columns, and add and read its test cases and results.",
        "resources": ["table"],
        "operations": ["ViewBasic", "ViewTests", "EditTests"],
        "effect": "allow",
    },
    {
        "name": "QualityWriter-TestDefinitions",
        "description": "Create and keep the definition each engine check is published under.",
        "resources": ["testDefinition"],
        "operations": ["Create", "EditAll"],
        "effect": "allow",
    },
)


def writer_bot(stage: str) -> str:
    return f"olf-quality-writer-{stage}"


def writer_credentials(stage: str) -> str:
    """The Secret, in the governance namespace, that holds the stage writer's JWT under `JWT_KEY`."""
    return f"openmetadata-quality-writer-{stage}"


def ensure_role(client: OpenMetadataClient) -> str:
    """The policy and role, put on every run so a drifted rule set is put back; returns the role's id."""
    client.request(
        "PUT",
        "/api/v1/policies",
        ok_statuses=_UPSERT,
        json_body={
            "name": POLICY,
            "displayName": "OpenLakeForge quality writer",
            "description": "Publish quality results to existing tables (ADR 0007). Nothing else.",
            "rules": list(RULES),
        },
    )
    role = client.request(
        "PUT",
        "/api/v1/roles",
        ok_statuses=_UPSERT,
        json_body={
            "name": ROLE,
            "displayName": "OpenLakeForge quality writer",
            "description": "Held only by the olf-quality-writer-<stage> bots.",
            "policies": [POLICY],
        },
    )
    if not role.get("id"):
        raise OpenMetadataError(f"OpenMetadata returned no id for role {ROLE}")
    return str(role["id"])


def _find_user(client: OpenMetadataClient, name: str) -> Mapping[str, Any] | None:
    try:
        return client.request("GET", f"/api/v1/users/name/{name}", params={"fields": "roles"})
    except OpenMetadataError as exc:
        if exc.status == 404:
            return None
        raise


def ensure_writer(
    client: OpenMetadataClient,
    route: Route,
    namespace: str,
    stage: str,
    role_id: str,
    environ: Mapping[str, str] | None = None,
) -> None:
    """The stage's bot holding the writer role and no other, and a working token in its Secret."""
    name = writer_bot(stage)
    user = _find_user(client, name)
    if user is None:
        created = client.request(
            "PUT",
            "/api/v1/users",
            ok_statuses=_UPSERT,
            json_body={
                "name": name,
                "email": f"{name}@open-metadata.org",
                "displayName": f"OpenLakeForge quality writer ({stage})",
                "isBot": True,
                "isAdmin": False,
                "botName": name,
                "roles": [role_id],
                "authenticationMechanism": {"authType": "JWT", "config": _JSON},
            },
        )
        if not created.get("id"):
            raise OpenMetadataError(f"Failed to create the {name} bot user: {created}")
        try:  # best effort, as for the automation bot: it lists the bot in the UI, where it can be revoked
            client.request(
                "PUT",
                "/api/v1/bots",
                ok_statuses=_UPSERT,
                json_body={"name": name, "botUser": name, "displayName": f"OpenLakeForge quality writer ({stage})"},
            )
        except ServiceClientError as exc:
            log.warn(f"Could not register the {name} bot in the UI: {exc}")
        user = _find_user(client, name) or created
    # A bot user put through /api/v1/users always gains DefaultBotRole, whose DataConsumerPolicy allows
    # EditDescription, EditTags and more on every resource (UserResource.addRolesToBot, 1.13.6). Replacing the roles
    # by PATCH is not subject to that, so the bot keeps exactly the writer role.
    if [role.get("id") for role in user.get("roles") or []] != [role_id]:
        client.request(
            "PATCH",
            f"/api/v1/users/{user['id']}",
            json_body=[{"op": "add", "path": "/roles", "value": [{"id": role_id, "type": "role"}]}],
            content_type="application/json-patch+json",
        )
    stored = k8s.secret_value(writer_credentials(stage), JWT_KEY, namespace, missing_ok=True, environ=environ)
    # A reconciler pass reads the Secret when its pod starts; a working token is kept so none is revoked mid-pass.
    if stored and _token_works(route, stored, user_id=user["id"]):
        return
    token = _bearer_token(client, user["id"], f"the {name} bot")
    k8s.write_secret(writer_credentials(stage), namespace, {JWT_KEY: token}, environ=environ)
    log.info(f"Stored the {name} token in Secret '{writer_credentials(stage)}'.")


def remove_writer(
    client: OpenMetadataClient, namespace: str, stage: str, environ: Mapping[str, str] | None = None
) -> None:
    """A stage that is no longer governed loses its bot, and with it every token the bot was issued, and its Secret."""
    name = writer_bot(stage)
    if _find_user(client, name) is not None:
        hard = {"hardDelete": "true", "recursive": "true"}
        client.request("DELETE", f"/api/v1/bots/name/{name}", params=hard, ok_statuses=(200, 404))
        client.request("DELETE", f"/api/v1/users/name/{name}", params=hard, ok_statuses=(200, 404))
        log.info(f"Removed the OpenMetadata bot {name}.")
    if k8s.secret_data(writer_credentials(stage), namespace, environ=environ) is not None:
        k8s.delete_secret(writer_credentials(stage), namespace, environ=environ)
        log.info(f"Revoked Secret '{writer_credentials(stage)}' from '{namespace}'.")


def ensure_quality_writers(
    client: OpenMetadataClient,
    route: Route,
    namespace: str,
    governed: Sequence[str],
    environ: Mapping[str, str] | None = None,
) -> None:
    """A writer for each governed stage, and none for any other stage the profile schema allows."""
    role_id = ensure_role(client)
    for stage in governed:
        ensure_writer(client, route, namespace, stage, role_id, environ)
    for stage in STAGES:
        if stage not in governed:
            remove_writer(client, namespace, stage, environ)
