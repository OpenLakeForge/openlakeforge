# ADR template

Copy everything below the line into `docs/adr/NNNN-<short-name>.md`, replace
the angle-bracketed text, and delete the guidance in italics. The rules
behind each section are in [How to write an ADR](README.md#how-to-write-an-adr).

---

# ADR NNNN: <the decision as a short phrase>

| | |
| --- | --- |
| Status | Binding · Proposed |
| Built | local ✔ · AWS ✘ (#issue) · Azure ✘ |

## Context

*At most 15 lines: the problem, and the constraints that still explain the
decision today. No snapshot of what the tree held when it was written, no
verification results.*

## Decision

**D1. <The rule, in one sentence.>** *A short paragraph: what binds and its
scope.* Enforced by: *the one file, check or test that holds the rule, for
example `olf check structure` or `release/identity-roles.yaml`.*

**D2. <The next rule.>** *...* Enforced by: *...*

*IDs are stable because code and other documents cite them. A new decision
takes the next free ID. A removed decision keeps its ID and reads
"**D3. Withdrawn (YYYY-MM-DD):** <reason, or the ID that absorbed it>." Never
renumber.*

## Consequences

- *Accepted costs and known limits, one or two lines each. A limit that rests
  on an open test or issue names the issue here, once.*

## Boundaries

*Optional.*

| Not decided here | Where |
| --- | --- |
| *<a nearby question this ADR does not own>* | *<the owning ADR or page>* |

## Alternatives rejected

| Option | Why not |
| --- | --- |
| *<option>* | *<one line>* |

## History

*Newest last, one line per change to a decision. A change that implements a
decision without changing it gets no line: `git log` and `CHANGELOG.md`
hold it.*

- YYYY-MM-DD (#issue): <which Dn was added, changed or withdrawn, and how>.
