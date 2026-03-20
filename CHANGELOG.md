# Changelog

## v1.1.0

- Auto-fix YAML plain scalars containing `: ` (colon-space) — no more parse errors for natural-language criteria
- Support `grading.rubric` eval format with `id`, `description`, `weight`, `pass_if` fields (normalized to flat criteria)
- Validate all eval cases (required fields, types) before making any API calls
- Support `.yml` extension alongside `.yaml` for eval files
- Add `scripts/test_validation.py` for local testing without API calls

## v1.0.0

- Initial release
- Discover and execute eval YAML test cases via `claude -p`
- Grade responses against criteria via separate `claude -p` call
- Post results as PR comment (upsert with HTML marker)
- Upload interactive eval-viewer HTML as artifact
- Configurable pass threshold with step failure
- GitHub Actions step summary with results table
