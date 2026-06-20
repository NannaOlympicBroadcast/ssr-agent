---
name: review-mode
description: A read-only code-review mode. Inspect a diff or branch and report correctness bugs and quality issues (reuse, simplification, efficiency) WITHOUT modifying files. Use when asked to review a PR/diff, confirm a fix is sound, or do a focused quality pass before committing.
---

# Review Mode

`review-mode` is a focused, **read-only** working mode for reviewing code changes.
It mirrors the behaviour of the Claude Code runtime's built-in review mode: while
it is active you analyse and report, but you do **not** edit files, run mutating
commands, or commit.

## When to use

- The user asks to review a pull request, a diff, or a branch.
- The user wants a second opinion on whether a change is correct before merging.
- A quality pass is wanted (reuse / simplification / efficiency / clarity) without
  changing behaviour yet.

## Operating rules

1. **Read-only.** Use `read_file`, `list_dir`, `search_context` and read-only
   shell commands (`git diff`, `git log`, `git status`, `git show`). Do not call
   `write_file`, do not run mutating commands, do not commit or push.
2. **Scope to the change.** Establish the diff first
   (`git diff`, or `git diff <base>...<head>`), then reason about what the change
   does and what it touches. Review the change, not the whole repository.
3. **Prioritise correctness.** Lead with bugs that affect behaviour: logic errors,
   off-by-one, null/None handling, error paths, races, resource leaks, security
   issues, and broken or missing tests.
4. **Then quality.** Note reuse opportunities, dead code, needless complexity,
   inefficient patterns, and unclear naming — but keep these separate from and
   below correctness findings.
5. **Be specific and grounded.** Cite `file:line` for every finding and quote the
   relevant code. Never invent issues; if you are unsure, say so and explain how
   to verify. Calibrate effort to what was asked (a few high-confidence findings
   for a quick pass; broader coverage for a deep review).

## Output format

Group findings by severity. For each finding give: location (`file:line`), a
one-line summary, why it matters, and a concrete suggested fix. End with a short
overall verdict (e.g. *looks good*, *minor nits*, *needs changes before merge*).
If the user then asks you to apply fixes, leave review mode and make the edits.
