# Skill Eval Action

A GitHub Action that evaluates [Claude Code skills](https://resources.anthropic.com/hubfs/The-Complete-Guide-to-Building-Skill-for-Claude.pdf) against YAML test cases with automated grading and PR reporting.

## Usage

### Single skill

```yaml
- uses: nicknikolakakis/skill-eval-action@v1
  with:
    skill-name: tf-guide
    skill-path: ./skills/tf-guide
    anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
```

### Multiple skills (static matrix)

Run skills in parallel - each skill gets its own job:

```yaml
name: Skill Eval
on:
  pull_request:
    paths:
      - 'skills/**'

permissions:
  contents: read
  pull-requests: write

jobs:
  eval:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    strategy:
      fail-fast: false
      matrix:
        skill:
          - tf-guide
          - k8s-operator-sdk
          - secure-gh-workflow
    steps:
      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6

      - uses: nicknikolakakis/skill-eval-action@v1
        with:
          skill-name: ${{ matrix.skill }}
          skill-path: skills/${{ matrix.skill }}
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          pass-threshold: '80'
```

### Auto-discover all skills (dynamic matrix)

Automatically find and evaluate all skills that have `evals/` directories - no need to hardcode skill names:

```yaml
name: Skill Eval
on:
  pull_request:
    paths:
      - 'skills/**'

permissions:
  contents: read
  pull-requests: write

jobs:
  discover:
    runs-on: ubuntu-latest
    outputs:
      skills: ${{ steps.discover.outputs.skills }}
      count: ${{ steps.discover.outputs.count }}
    steps:
      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6
        with:
          persist-credentials: false
          sparse-checkout: skills

      - name: Discover skills with evals
        id: discover
        run: python ${{ github.action_path }}/scripts/discover.py skills
        # Or inline without the script:
        # run: |
        #   skills=$(find skills -name "*.yaml" -path "*/evals/*" -exec dirname {} \; | xargs -I{} dirname {} | xargs -I{} basename {} | sort -u | jq -R -s -c 'split("\n") | map(select(. != ""))')
        #   echo "skills=$skills" >> "$GITHUB_OUTPUT"

      - name: Summary
        run: echo "Found ${{ steps.discover.outputs.count }} skills with evals"

  eval:
    needs: discover
    if: needs.discover.outputs.count > 0
    runs-on: ubuntu-latest
    timeout-minutes: 30
    strategy:
      fail-fast: false
      matrix:
        skill: ${{ fromJSON(needs.discover.outputs.skills) }}
    steps:
      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6

      - uses: nicknikolakakis/skill-eval-action@v1
        with:
          skill-name: ${{ matrix.skill }}
          skill-path: skills/${{ matrix.skill }}
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          pass-threshold: '80'
```

### Only evaluate changed skills

Combine with [dorny/paths-filter](https://github.com/dorny/paths-filter) or git diff to only eval skills that were modified in the PR:

```yaml
jobs:
  changed:
    runs-on: ubuntu-latest
    outputs:
      skills: ${{ steps.filter.outputs.skills }}
    steps:
      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6
        with:
          persist-credentials: false

      - name: Find changed skills with evals
        id: filter
        run: |
          skills=$(git diff --name-only origin/main...HEAD -- 'skills/' | cut -d/ -f2 | sort -u | while read s; do
            [ -d "skills/$s/evals" ] && echo "$s"
          done | jq -R -s -c 'split("\n") | map(select(. != ""))')
          echo "skills=$skills" >> "$GITHUB_OUTPUT"

  eval:
    needs: changed
    if: needs.changed.outputs.skills != '[]'
    runs-on: ubuntu-latest
    timeout-minutes: 30
    strategy:
      fail-fast: false
      matrix:
        skill: ${{ fromJSON(needs.changed.outputs.skills) }}
    steps:
      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6

      - uses: nicknikolakakis/skill-eval-action@v1
        with:
          skill-name: ${{ matrix.skill }}
          skill-path: skills/${{ matrix.skill }}
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
```

## Parallelism

The action evaluates **one skill per invocation**. Parallelism comes from GitHub Actions matrix strategy:

| Approach | Skills in parallel | How |
|----------|:-:|-----|
| Static matrix | Up to 256 | List skills in `matrix.skill` |
| Dynamic matrix | Up to 256 | Use discover step + `fromJSON()` |
| Changed only | Varies | Filter by git diff |
| Sequential | 1 | No matrix (not recommended for >3 skills) |

Within a single skill, eval cases run sequentially to avoid Anthropic API rate limits.

## Inputs

| Input | Required | Default | Description |
|-------|:--------:|---------|-------------|
| `skill-name` | Yes | - | Name of the skill to evaluate |
| `skill-path` | Yes | - | Path to the skill directory (must contain `SKILL.md` and `evals/`) |
| `anthropic-api-key` | Yes | - | Anthropic API key for the `claude` CLI |
| `pass-threshold` | No | `80` | Minimum pass rate (0-100) to succeed |
| `timeout` | No | `120` | Timeout per eval case in seconds |
| `post-comment` | No | `true` | Post results as a PR comment |
| `github-token` | No | `${{ github.token }}` | Token for PR comments |
| `upload-viewer` | No | `true` | Upload eval-viewer HTML as an artifact |
| `node-version` | No | `22` | Node.js version for claude CLI installation |

## Outputs

| Output | Description |
|--------|-------------|
| `pass-rate` | Overall pass rate as percentage (0-100) |
| `passed` | Total criteria passed |
| `total` | Total criteria evaluated |
| `cases-run` | Number of eval cases executed |

## How it works

```
eval YAML -> claude -p (execute) -> claude -p (grade) -> summary.json -> PR comment + artifact
```

1. **Discovers** eval YAML files in `<skill-path>/evals/`
2. **Executes** each case via `claude -p` with skill content injected
3. **Grades** each response against criteria via a separate `claude -p` call
4. **Aggregates** results and writes a GitHub Actions step summary
5. **Posts** a PR comment with pass/fail table and failed criteria details
6. **Uploads** an interactive eval viewer as an artifact
7. **Fails** the step if pass rate is below threshold

## Eval case format

Place YAML files in `<skill-path>/evals/`:

```yaml
# evals/001-basic-usage.yaml
name: Basic usage
prompt: "The user prompt that should trigger and test this skill"
files:                          # optional - temp files created before the test
  - path: "main.tf"
    content: |
      resource "aws_instance" "web" {}
criteria:                       # success criteria - ALL must pass
  - "Output contains a valid resource block"
  - "Uses for_each, not count, for multiple resources"
expect_skill: true              # optional - default true
timeout: 120                    # optional - default from action input
```

Include at least one negative trigger case (`expect_skill: false`).

## PR comment

The action posts (or updates) a PR comment with:

- Pass/fail table with per-case results
- Collapsible failed criteria with evidence
- Eval metadata (time, tokens, cost, threshold)

Comments are upserted using an HTML marker - re-runs update the existing comment instead of creating duplicates.

## Non-determinism and flakiness

LLM-based evals are non-deterministic. Each run, Claude generates a slightly different response, and the grader evaluates it slightly differently. The same skill without changes may produce different pass rates across runs.

This is why:
- The default `pass-threshold` is `80` not `100`
- The [agentskills.io best practices](https://agentskills.io/skill-creation/evaluating-skills) say "occasional flakiness is expected"
- Multiple runs + aggregation gives a more reliable picture

Options to reduce flakiness:
1. **Relax criteria** - make them less brittle (e.g., "uses SHA pinning or explains how to resolve SHAs" instead of "all actions pinned to 40-char SHA")
2. **Run multiple times and average** - aggregate results across runs for a stable signal
3. **Lower threshold** - accept that 70-80% is a realistic pass rate for LLM evals

## Cost considerations

Each eval case makes **2 API calls** (execute + grade). A skill with 5 cases = 10 API calls. Set appropriate `timeout` values to limit runaway token usage. Use the "changed only" pattern to avoid evaluating unchanged skills on every PR.

## Requirements

- `ANTHROPIC_API_KEY` as a repository secret
- Eval YAML files in the skill's `evals/` directory
- Skills must follow the [Agent Skills](https://agentskills.io/specification) format

## License

MIT
