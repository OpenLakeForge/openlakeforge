# Add, change or remove a member

How to give a person access, change their role or take it away. People and
roles live in your GitHub organization, so there is no `olf` command for users
and none of these steps needs a deploy.

| To | Do this in GitHub | What happens |
| --- | --- | --- |
| Give the first administrators access | Put them in the team mapped to `platform-admin` | They hold `platform-admin` at their first sign-in. Nothing is created at deploy. |
| Add a person | Add them to the organization and to a mapped team | They sign in with GitHub on the portal. GitHub owns their password, two-factor authentication and recovery. |
| Change a role | Move them between mapped teams | Their next token carries the new teams. |
| Remove every role | Remove them from every mapped team | Dex refuses their next sign-in or refresh. |
| Offboard | Remove them from the organization | The same, and GitHub ends their organization access everywhere else at once. |

Only members of a mapped team can sign in. Being an organization owner grants
nothing in OpenLakeForge.

## How fast a change takes effect

A person with no open session is affected at their next sign-in. For a person
who is signed in:

| Where | Delay | Why |
| --- | --- | --- |
| Portal, Dagster, Trino UI and every route (the perimeter) | About 4 to 5 minutes | The perimeter refreshes the session against Dex at most every 4 minutes, and Dex re-reads team membership on a refresh. The change shows on the first request after a refresh; access tokens last 5 minutes. |
| OpenMetadata roles | Up to 5 minutes plus the run | A CronJob, `openmetadata-role-sync`, reads the mapped teams every 5 minutes and sets the administrator flag and the `DataSteward` role. It needs the GitHub App (the Secret's `app-id` and `private-key`); with a hand-made OAuth App it is skipped and OpenMetadata users stay at the read baseline. |
| Superset roles | At most 15 minutes (designed; not measured) | Superset's own session is designed to last 15 minutes without sliding, and its roles are read again at each sign-in. The perimeter refuses a removed person first. |

Measured once, against real GitHub: a member moved to another team lost the
old role on the first request after the 4-minute refresh. The bound is "the
next request at least 4 minutes after sign-in or the last refresh", not a
timer, so a person who stays idle is refused at their next request.

A person's session also ends 30 minutes after their last request and 10 hours
after sign-in, whatever else is true.

> [!NOTE]
> Ownership outlives a role. In OpenMetadata, the owner of an asset keeps full
> control of it after a demotion until the ownership is reassigned. Reassign
> the assets of someone you demote or offboard.

## Check a change took effect

1. Make the change in GitHub and note the time.
2. In a fresh private window, sign in as the person (or use their open
   session) and open a service their new role should deny.
3. Expect the "Access is role-restricted" page, or Dex's refusal when they have
   no role at all.
