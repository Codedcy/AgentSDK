# Agent SDK Author History Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite every existing `master` commit from `Codex <codex@local>` to `Codedcy <1017672929@qq.com>`, preserve commit content and dates, and repair every tracked documentation reference to the rewritten commit hashes.

**Architecture:** Create and verify an offline Git bundle before changing refs. Rewrite only the local `master` commit identities while preserving trees, messages, topology, and dates; derive an exact old-to-new hash map from the linear history; then update documentation references and record the rewrite. Keep the remote unchanged until a separately approved force-push.

**Tech Stack:** Git, PowerShell, Python 3 standard library, pytest, Ruff

## Global Constraints

- Target identity is exactly `Codedcy <1017672929@qq.com>` for both Author and Committer.
- Preserve all 333 pre-rewrite commit trees, messages, parent order, author dates, and committer dates.
- Preserve a verified offline bundle before rewriting any ref.
- Do not push, force-push, create tags, publish packages, or change SDK behavior.
- Replace tracked-text hashes only when a token is a unique prefix of an actual old commit.
- Re-run documentation gates and the full supported Python test suite after the rewrite.

---

### Task 1: Backup and Rewrite Commit Identities

**Files:**
- Create outside history: `.git/backups/pre-author-rewrite-2026-07-22.bundle`
- Create temporarily: `refs/rewrites/pre-author-master`
- Modify: Git `master` history only

**Interfaces:**
- Consumes: clean `master` at `b32a424`, global Git identity, `origin/master`
- Produces: rewritten linear `master`, verified backup bundle, old/new commit lists

- [x] **Step 1: Verify the preconditions**

Run `git status --short`, identity counts, merge count, branch/remote refs, and confirm the exact target global identity.

- [x] **Step 2: Create and verify the offline backup**

Create the bundle with `git bundle create .git/backups/pre-author-rewrite-2026-07-22.bundle --all`, then run `git bundle verify` and retain the old root-to-head history through the temporary rewrite ref until mapping and verification finish.

- [x] **Step 3: Rewrite Author and Committer identities**

Use Git's history filter on local `master` only. Replace the old name/email conditionally while leaving dates, messages, trees, and topology unchanged.

- [x] **Step 4: Verify the rewritten history**

Require 333 commits, zero merge changes, zero old Author/Committer identities, 333 target Author/Committer identities, and pairwise equality of trees/messages/dates between old and new histories.

### Task 2: Repair Commit References and Release Records

**Files:**
- Modify: `.superpowers/sdd/*.md` files containing mapped hashes
- Modify: `docs/**/*.md` files containing mapped hashes
- Modify if matched: `README.md`
- Modify if matched: `CHANGELOG.md`
- Modify: `tests/docs/test_v01_release_ledger.py`
- Modify: `.superpowers/sdd/progress.md`
- Modify: `docs/plans/releases/v0.1.md`
- Add: `docs/superpowers/plans/2026-07-22-agent-sdk-author-history-rewrite.md`

**Interfaces:**
- Consumes: exact root-to-head old/new commit lists from Task 1
- Produces: no remaining tracked-text token that uniquely names an old commit; an auditable rewrite record

- [x] **Step 1: Generate the exact mapping**

Pair the equal-length linear old/new histories and reject any pair whose tree, message, author date, or committer date differs.

- [x] **Step 2: Replace only verified commit tokens**

For hexadecimal tokens of length 7-40 in tracked UTF-8 text, replace only a unique old-commit prefix and preserve the token's original abbreviation length. The release-ledger contract test is included because its assertions intentionally mirror documented checkpoint hashes.

- [x] **Step 3: Record the identity rewrite**

Add the target identity, rewritten commit count, backup location, validation results, remote boundary, and rewritten release/checkpoint hashes to the v0.1 ledger and progress log.

- [x] **Step 4: Commit the repaired documentation**

Commit as `docs: record author history rewrite` using the configured target identity.

### Task 3: Verify and Prepare Remote Handoff

**Files:**
- Test: `tests/docs`
- Test: full project

**Interfaces:**
- Consumes: rewritten history and repaired documentation
- Produces: clean local `master` ready for an explicitly approved force-push

- [x] **Step 1: Run focused documentation verification**

Run documentation tests, Ruff on changed Python tests/scripts if any, the old-hash residue audit, identity audit, and `git diff --check`.

- [x] **Step 2: Run the complete supported test gate**

Run the complete pytest suite under supported Python with the asyncio plugin and require zero failures.

- [x] **Step 3: Remove temporary local rewrite refs**

After the bundle and mapping are verified, delete `refs/original` and temporary rewrite refs so `master` is the only local branch history while keeping the bundle.

- [x] **Step 4: Stop before remote mutation**

Report the exact local/remote divergence and request explicit approval before `git push --force-with-lease origin master`.
