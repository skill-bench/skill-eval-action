# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A GitHub Action (composite) that evaluates Claude Code skills against YAML test cases with automated grading and PR reporting. Published to GitHub Marketplace as `skill-bench/skill-eval-action`.

## Commands

```bash
# Lint Python
ruff check scripts/

# Security/best-practice lint for GitHub Actions workflows
zizmor action.yml .github/workflows/*.yml

# No unit tests — testing is done via CI on PRs
```

## Architecture

The action runs a **two-stage eval pipeline**: execute (get LLM response) → grade (evaluate against criteria), each as a separate `claude -p` API call.

**Pipeline flow** (orchestrated by `action.yml` composite steps):

1. `scripts/eval.py` — Core pipeline. Discovers YAML eval cases in `<skill-path>/evals/`, runs each sequentially (to avoid rate limits), grades responses, writes per-case outputs and `summary.json`. Retry logic with exponential backoff wraps both execute and grade calls via `_run_claude()`.
2. `scripts/generate_viewer.py` — Reads `summary.json` + per-case files, embeds data into `scripts/viewer.html` template to produce a self-contained offline-viewable HTML artifact.
3. `scripts/post_comment.py` — Upserts a PR comment (using `<!-- skill-eval: {NAME} -->` marker to avoid duplicates) via `gh api`.
4. `scripts/check_threshold.py` — Compares pass rate against threshold, exits non-zero if below.
5. `scripts/discover.py` — Utility for dynamic matrix workflows; discovers all skills with `evals/` directories.

**Key design decisions:**
- Skill content from `SKILL.md` is prepended to prompts for positive trigger cases; negative cases run raw.
- Grading truncates responses to 10KB; viewer truncates to 5KB.
- Cases run sequentially, not in parallel, to stay within API rate limits.
- If grading fails after retries, all criteria are marked as failed (not silently skipped).

## Eval Case Format

YAML files in `<skill-path>/evals/` with fields: `name`, `prompt`, `files` (optional), `criteria` (list), `expect_skill` (bool), `timeout`.

## Dependencies

- **Runtime:** Python 3.13, Node.js 22, PyYAML, `@anthropic-ai/claude-code` (npm)
- **Dev:** ruff, zizmor, uv

## CI Workflows

- `ci.yml` — ruff lint, action.yml schema validation, zizmor security checks (on PR/push to main)
- `release.yml` — Updates major version tag (e.g., `v1` → latest `v1.x.y`) on release creation
