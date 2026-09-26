---
name: "jev-decide-html-publish"
description: "Use when deciding whether a html-publish branch is ready to merge, reading a `just jev-merge` verdict, or recording how an escalation turned out."
---

# Jev decisions for html-publish

`jevgate` asks Jev narrow questions about this branch and turns the answers into one advisory verdict. A verdict never grants merge or deploy authority.

## Run the checks in order

| Order | Command | Question it answers |
| --- | --- | --- |
| 1 | `just check` | Does it build, lint, and pass its tests? It stays offline and never calls TypeSafe |
| 2 | `verify-html-publish` | Does the running app do what the feature map says? |
| 3 | `just jev-merge` | Does this evidence need a person or a stronger model to look? It runs step 1, or reuses its record for this clean HEAD |
| any | `just jev explain <run-id>` | What was sent, what Jev answered, and why the verdict follows |
| any | `just jev doctor` | Are the runtime, key, and permission ready? |

Commit everything before step 3; a dirty tree gives `insufficient`. `just jev-merge --help` lists every question and band. Read them there rather than copying them. When several runs interleave, use the run ID each run prints instead of `last`.

## Act on the verdict

| Verdict | Exit | Next move |
| --- | --- | --- |
| `pass` | 0 | Continue. It is not permission to merge |
| `escalate` | 10 | Review: a person, or the named reasoning review, inspects each numbered reason and its cited hunk |
| `block` | 11 | Fix the deterministic failure: a red check, a conflict, conflict markers, or a secret in the payload. Nothing was sent |
| `insufficient` | 12 | Gather what it names: run the check, commit, fetch the base, or add the missing proof |

An engine error exits 1 with `error.kind`. Fix what it names, then rerun.

## Read the probabilities

- Deep in the adverse band: inspect the cited evidence, then fix a confirmed defect
- Near a threshold: gather relevant evidence or ask for review. Rerunning an identical request hits the cache and adds no evidence
- A flagged risk with no pack yet: hand it to a person

## Record the outcome

After an escalation is resolved, or when a passed change later proves wrong:

`just jev label <run-id> --scope gate --outcome good|bad|unknown --by human|model|reproduced --evidence "<what the review found>"`

The outcome judges the captured change, not whether Jev agreed. No later fix means `unknown`, never `good`. Add `--admit` whenever the gate was wrong in either direction, then commit `.jev/cases/<run-id>/` when `[privacy] commit_cases` is true.

## Improve the questions

Questions and thresholds change only in a requested maintenance session, through `maintain-a-jev-cli-decision-wrapped-in-a-skill` once it exists. Until then, keep labeling.

## Setup and upgrades

The project configuration records Pascal's permission to send selected evidence and commit admitted cases. Preview it with `just jev-merge --dry-run`.

Use `lefthook install` to enable the local advisory pre-push hook. Its coverage rules live in the [behavior contract](../../../docs/contract.md#developer-review-tooling).

To upgrade, invoke `create-a-jev-cli-decision-wrapped-in-a-skill`.
