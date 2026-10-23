# Map teams to roles

How `spec.identity.github.teams` turns GitHub teams into the four OpenLakeForge
roles, and what to check when a person gets the wrong one.

## The mapping

```yaml
spec:
  identity:
    issuer: github
    github:
      organization: my-org
      teams:
        platform-admin: [olf-platform-admins]
        data-engineer: [olf-data-engineers]
        analyst: [olf-analysts, partner-analysts]
        viewer: [olf-viewers]
```

| Rule | Detail |
| --- | --- |
| Keys | The canonical roles: `platform-admin`, `data-engineer`, `analyst`, `viewer`. Any other name is rejected. |
| Values | A non-empty list of team slugs, as they appear in the team URL. `olf` lowercases them. |
| Repeats | A slug may not repeat within a list, and one team may not map to two roles. |
| Omitted roles | A role you leave out is held by nobody. At least one role must be mapped. |
| Organization | The login exactly as GitHub shows it, case included. |

Several teams can feed one role. A person in teams of more than one role holds
the highest of them: the grants are cumulative, so a `platform-admin` also
holds everything below. Roles and what they open are in
[Roles and permissions](../../reference/roles-and-permissions.md).

## Apply a change

The mapping is part of the deployment, so changing which teams feed a role
takes a deploy:

```bash
olf deploy --provider local
```

Changing who is in a team does not: see
[Add, change or remove a member](manage-members.md).

## Check a person's role

Sign in as that person in a fresh private window and open Superset and
Dagster. The table in
[Roles and permissions](../../reference/roles-and-permissions.md) says what
each role opens. A person who is refused at sign-in is in no mapped team, or
the GitHub App is not installed:
[Troubleshooting](connect-github.md#troubleshooting).

Only teams in the organization named by `organization` count. A team
elsewhere with the same slug grants nothing.
