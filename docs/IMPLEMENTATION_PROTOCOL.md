# Implementation claim and draft-PR protocol

This is the required protocol for future write-capable maintainers. It is
documented before implementation so later code must preserve the invariant.

## Core invariant

**An autonomous maintainer must not modify implementation source files until it
has won the remote implementation claim for the issue and a draft pull request
already exists on GitHub.**

"Check for an existing PR, then start coding" is insufficient because two
machines can perform that check at the same time. The claim itself must be a
remote atomic operation.

Every autonomous implementation must be tied to an issue first. Direct
untracked implementation PRs are not allowed.

## Canonical remote claim

The default implementation branch for issue `#N` is:

```text
maintainerd/issue-N
```

All maintainers competing for the same issue use the same branch name.

The controller, not the model, performs the claim:

1. Fetch the latest target branch.
2. Create a local branch from the recorded base commit.
3. Create an **empty claim commit** with no source-tree changes, for example:

   ```text
   chore: claim #11 for implementation

   Maintainer: mira
   Maintainerd-Issue: 11
   Maintainerd-Claim: <unique claim id>
   ```

4. Push that commit to `refs/heads/maintainerd/issue-11` **without force**.

Git's remote ref creation is the lock. If two agents race, only one first push
can create the canonical branch. The loser must stop before editing code,
refresh GitHub state, and join the existing implementation discussion instead.

No force push, ref replacement, rebase-over-remote, or fallback branch name is
allowed after losing the claim.

## Draft PR before code

Immediately after winning the branch claim, the trusted controller creates a
**draft PR** for that canonical branch and links it to the issue.

The draft PR is the first public implementation artifact. The empty claim
commit exists only because GitHub needs a distinct head ref/commit before a PR
can be opened.

Until GitHub confirms the draft PR exists:

- the coding agent remains read-only;
- no implementation file may be edited;
- no dependency may be changed;
- no tests that mutate the worktree may be run;
- no alternative implementation branch may be invented.

If draft-PR creation fails, the implementation does not begin.

A claim-only branch with no PR is considered incomplete. If the claim commit is
still the only remote commit and no PR appeared within a small recovery window,
maintainerd may safely recover or release that abandoned claim. It must never
delete a branch containing implementation commits as automatic cleanup.

## Commit-stream workflow

Once the draft PR exists, the winning maintainer receives a writable task
worktree on that exact branch.

Work is then accumulated visibly on the already-open draft PR:

```text
empty claim commit
    ↓
draft PR exists
    ↓
implementation commit 1 → push
    ↓
implementation commit 2 → push
    ↓
test/regression commit   → push
    ↓
fix/review commit        → push
    ↓
CI green + work complete
    ↓
mark ready for review
```

Commits should be coherent engineering steps, not one commit per keystroke. A
completed implementation must not be hidden locally and uploaded as one giant
final commit merely to make the PR appear late.

Push after each meaningful commit so other maintainers can see current work,
avoid duplicate implementation, comment on the approach, and review partial
progress.

While the PR is open:

- never force-push;
- never rewrite published commit history;
- prefer additive fix-up commits;
- keep the PR draft while implementation is incomplete;
- mark the PR ready for review when implementation completes;
- do not merge autonomously.

## Ownership

While implementation is incomplete, the open draft PR on the canonical issue branch is the implementation lease. When implementation completes, maintainerd marks it ready for review and releases the implementation phase without merging it.

Other maintainers may still:

- read the branch and PR;
- comment on design;
- contribute new evidence;
- review commits;
- point out bugs or missing tests.

They must not create a competing default implementation.

If the original maintainer stops, the work remains visible and resumable rather
than disappearing in a private worktree.

Human-authorized parallel implementations are an explicit exception. They must
use clearly separate alternate branches and PRs and are never created merely
because an agent disagrees with the current implementation.

## Recovery and races

The controller must design every transition to be restart-safe:

- branch creation is won remotely, not inferred from local SQLite;
- draft PR creation is idempotent by branch/head plus hidden maintainerd marker;
- restart after branch push but before PR recording must discover the existing
  remote branch/PR rather than create another;
- losing a branch-creation race is a normal coordination result, not an error
  that triggers a differently named branch;
- an existing draft PR always beats stale local state.

This makes GitHub itself the cross-machine coordination authority while keeping
the controller simple.
