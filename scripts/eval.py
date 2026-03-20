#!/usr/bin/env python3
"""Core eval pipeline: discover → execute → grade → aggregate.

Single script that runs the entire eval pipeline for CI.
Reads config from environment variables, writes results to WORKSPACE,
and sets GitHub Actions outputs.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------

SKILL_NAME = os.environ["SKILL_NAME"]
SKILL_PATH = Path(os.environ["SKILL_PATH"])
WORKSPACE = Path(os.environ["WORKSPACE"])
EVAL_TIMEOUT = int(os.environ.get("EVAL_TIMEOUT", "120"))
PASS_THRESHOLD = float(os.environ.get("PASS_THRESHOLD", "80"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
RETRY_DELAY = int(os.environ.get("RETRY_DELAY", "10"))


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------

def _safe_yaml_load(text: str, _max_fixes: int = 50) -> dict:
    """Load YAML, auto-quoting plain scalar values that contain ': '.

    YAML plain scalars cannot contain ': ' (colon-space).  This is a
    constant source of errors for eval authors writing natural-language
    criteria.  When PyYAML raises "mapping values are not allowed here"
    we locate the offending line, wrap its value in double quotes, and
    retry — up to ``_max_fixes`` times so that multiple lines in the
    same file are handled in a single call.
    """
    lines = text.split("\n")
    fixed_lines: set[int] = set()

    for _ in range(_max_fixes):
        try:
            return yaml.safe_load("\n".join(lines))
        except yaml.scanner.ScannerError as exc:
            if (
                exc.problem == "mapping values are not allowed here"
                and exc.problem_mark is not None
            ):
                err_line = exc.problem_mark.line  # 0-indexed
                if err_line in fixed_lines:
                    raise  # already tried this line — bail out
                line = lines[err_line]
                m = re.match(r"^(\s+\w[\w_-]*):\s+(.+)$", line)
                if m:
                    value = m.group(2)
                    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
                    lines[err_line] = f'{m.group(1)}: "{escaped}"'
                    fixed_lines.add(err_line)
                    continue
            raise

    return yaml.safe_load("\n".join(lines))


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_evals(skill_path: Path) -> list[dict]:
    """Read all .yaml/.yml eval files from the skill's evals/ directory."""
    evals_dir = skill_path / "evals"
    if not evals_dir.is_dir():
        return []
    yaml_files = sorted(
        list(evals_dir.glob("*.yaml")) + list(evals_dir.glob("*.yml")),
        key=lambda p: p.name,
    )
    cases = []
    for yaml_file in yaml_files:
        try:
            case = _safe_yaml_load(yaml_file.read_text())
        except yaml.YAMLError as exc:
            print(
                f"::error file={yaml_file}::Failed to parse eval YAML: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)
        if not isinstance(case, dict):
            print(
                f"::error file={yaml_file}::Eval YAML must be a mapping, got {type(case).__name__}",
                file=sys.stderr,
            )
            sys.exit(1)
        # Normalize rubric format → flat criteria list
        if "criteria" not in case and "grading" in case:
            grading = case["grading"]
            rubric = grading.get("rubric", []) if isinstance(grading, dict) else []
            criteria = []
            for entry in rubric:
                if isinstance(entry, dict):
                    desc = entry.get("description", "")
                    pass_if = entry.get("pass_if", "")
                    if pass_if:
                        criteria.append(f"{desc} — PASS IF: {pass_if}")
                    elif desc:
                        criteria.append(desc)
                elif isinstance(entry, str):
                    criteria.append(entry)
            case["criteria"] = criteria
            # Preserve per-case pass threshold if specified
            if isinstance(grading, dict) and "pass_threshold" in grading:
                case.setdefault("case_pass_threshold", grading["pass_threshold"])

        case.setdefault("name", yaml_file.stem)
        case.setdefault("expect_skill", True)
        case.setdefault("timeout", EVAL_TIMEOUT)
        case["_source"] = str(yaml_file)
        cases.append(case)
    return cases


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_cases(cases: list[dict]) -> list[str]:
    """Validate eval cases and return a list of error strings (empty = OK)."""
    errors: list[str] = []

    for case in cases:
        src = case.get("_source", "unknown")
        name = case.get("name", "unnamed")
        prefix = f"{src} ({name})"

        # prompt — required, must be a non-empty string
        prompt = case.get("prompt")
        if prompt is None:
            errors.append(f"{prefix}: missing required field 'prompt'")
        elif not isinstance(prompt, str) or not prompt.strip():
            errors.append(f"{prefix}: 'prompt' must be a non-empty string")

        # criteria — required, list of strings
        criteria = case.get("criteria")
        if criteria is None:
            errors.append(f"{prefix}: missing required field 'criteria' (list of strings)")
        elif not isinstance(criteria, list) or len(criteria) == 0:
            errors.append(f"{prefix}: 'criteria' must be a non-empty list")
        else:
            for i, c in enumerate(criteria):
                if not isinstance(c, str):
                    errors.append(
                        f"{prefix}: criteria[{i}] must be a string, got {type(c).__name__}. "
                        "If using a rubric structure, flatten to a list of plain strings."
                    )

        # files — optional, list of dicts with 'path'
        files = case.get("files")
        if files is not None:
            if not isinstance(files, list):
                errors.append(f"{prefix}: 'files' must be a list of {{path, content}} objects")
            else:
                for i, f in enumerate(files):
                    if not isinstance(f, dict):
                        errors.append(f"{prefix}: files[{i}] must be a mapping with 'path' key")
                    elif "path" not in f:
                        errors.append(f"{prefix}: files[{i}] missing required 'path' key")

        # expect_skill — optional, bool
        es = case.get("expect_skill")
        if es is not None and not isinstance(es, bool):
            errors.append(f"{prefix}: 'expect_skill' must be true or false, got {type(es).__name__}")

        # timeout — optional, number
        to = case.get("timeout")
        if to is not None and not isinstance(to, (int, float)):
            errors.append(f"{prefix}: 'timeout' must be a number, got {type(to).__name__}")

    return errors


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _run_claude(prompt: str, work_dir: Path, timeout: int) -> subprocess.CompletedProcess:
    """Run claude -p with retries on timeout/error."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = subprocess.run(
                ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose"],
                capture_output=True, text=True,
                timeout=timeout, cwd=str(work_dir), env=env,
            )
            # Check for empty response (API error, rate limit)
            if result.returncode != 0 and attempt < MAX_RETRIES:
                delay = RETRY_DELAY * attempt
                print(f"  ::warning::Attempt {attempt}/{MAX_RETRIES} failed (exit {result.returncode}), retrying in {delay}s...")
                time.sleep(delay)
                continue
            return result
        except subprocess.TimeoutExpired:
            if attempt < MAX_RETRIES:
                delay = RETRY_DELAY * attempt
                print(f"  ::warning::Attempt {attempt}/{MAX_RETRIES} timed out after {timeout}s, retrying in {delay}s...")
                time.sleep(delay)
                continue
            raise

    return result  # type: ignore


def _parse_stream_json(stdout: str) -> dict:
    """Parse claude stream-json output into structured data."""
    response_text = ""
    total_tokens = 0
    cost_usd = 0.0
    skill_triggered = False

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "assistant":
            for content in event.get("message", {}).get("content", []):
                if content.get("type") == "text":
                    response_text += content.get("text", "")
                elif content.get("type") == "tool_use":
                    if content.get("name") == "Skill" and SKILL_NAME in json.dumps(content.get("input", {})):
                        skill_triggered = True
        elif event.get("type") == "result":
            usage = event.get("usage", {})
            total_tokens = (
                usage.get("input_tokens", 0)
                + usage.get("output_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
            )
            cost_usd = event.get("total_cost_usd", 0)
            if not response_text:
                response_text = event.get("result", "")

    return {
        "response_text": response_text,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
        "skill_triggered": skill_triggered,
    }


def execute_case(case: dict, skill_content: str, case_dir: Path) -> dict:
    """Run a single eval case via claude -p with retries."""
    case_dir.mkdir(parents=True, exist_ok=True)

    # Create temp dir with any specified files
    work_dir = Path(tempfile.mkdtemp(prefix=f"eval-{case['name']}-"))
    for file_spec in case.get("files", []):
        fp = work_dir / file_spec["path"]
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(file_spec.get("content", ""))

    # Inject skill content for positive trigger cases
    raw_prompt = case.get("prompt", "")
    if skill_content and case.get("expect_skill", True):
        prompt = (
            f"Follow these skill instructions when responding:\n\n"
            f"<skill-instructions>\n{skill_content}\n</skill-instructions>\n\n"
            f"User request: {raw_prompt}"
        )
    else:
        prompt = raw_prompt

    start = time.time()

    try:
        result = _run_claude(prompt, work_dir, case.get("timeout", EVAL_TIMEOUT))
        elapsed = time.time() - start
        parsed = _parse_stream_json(result.stdout)

        # Write outputs
        (case_dir / "response.md").write_text(parsed["response_text"])
        (case_dir / "timing.json").write_text(json.dumps({
            "total_tokens": parsed["total_tokens"],
            "duration_seconds": round(elapsed, 1),
        }, indent=2))
        (case_dir / "eval_metadata.json").write_text(json.dumps({
            "prompt": raw_prompt,
            "criteria": case.get("criteria", []),
            "expect_skill": case.get("expect_skill", True),
            "skill_triggered": parsed["skill_triggered"],
        }, indent=2))

        return {
            "name": case["name"], "status": "completed",
            "elapsed": round(elapsed, 1), "tokens": parsed["total_tokens"],
            "cost_usd": parsed["cost_usd"],
            "skill_triggered": parsed["skill_triggered"],
            "response": parsed["response_text"],
        }
    except subprocess.TimeoutExpired:
        return {"name": case["name"], "status": "timeout", "elapsed": round(time.time() - start, 1), "tokens": 0, "response": ""}
    except Exception as e:
        return {"name": case["name"], "status": "error", "elapsed": 0, "tokens": 0, "response": "", "error": str(e)}
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

def grade_case(case: dict, exec_result: dict, case_dir: Path) -> dict:
    """Grade an executed eval case via claude -p with retries."""
    criteria = case.get("criteria", [])
    criteria_text = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(criteria))
    response = exec_result.get("response", "(No response captured)")

    if len(response) > 10000:
        response = response[:10000] + "\n\n... (truncated at 10KB) ..."

    grader_prompt = f"""You are an eval grader. Grade this skill response against criteria. Be strict - FAIL if evidence is weak or superficial.

CRITERIA:
{criteria_text}

RESPONSE:
{response}

Output ONLY valid JSON in this exact format (no markdown, no explanation):
{{
  "expectations": [
    {{"text": "criterion text", "passed": true/false, "evidence": "specific quote or description"}}
  ],
  "summary": {{"passed": N, "failed": N, "total": N, "pass_rate": 0.0}}
}}"""

    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = subprocess.run(
                ["claude", "-p", grader_prompt, "--output-format", "text"],
                capture_output=True, text=True, timeout=60, env=env,
            )
            output = result.stdout.strip()
            if "```json" in output:
                output = output.split("```json")[1].split("```")[0].strip()
            elif "```" in output:
                output = output.split("```")[1].split("```")[0].strip()

            grading = json.loads(output)
            (case_dir / "grading.json").write_text(json.dumps(grading, indent=2) + "\n")
            return grading

        except (json.JSONDecodeError, subprocess.TimeoutExpired) as e:
            if attempt < MAX_RETRIES:
                delay = RETRY_DELAY * attempt
                print(f"  ::warning::Grading attempt {attempt}/{MAX_RETRIES} failed ({e.__class__.__name__}), retrying in {delay}s...")
                time.sleep(delay)
                continue
            # Final attempt failed
            fallback = {
                "expectations": [{"text": c, "passed": False, "evidence": f"Grading failed after {MAX_RETRIES} attempts: {e}"} for c in criteria],
                "summary": {"passed": 0, "failed": len(criteria), "total": len(criteria), "pass_rate": 0.0},
            }
            (case_dir / "grading.json").write_text(json.dumps(fallback, indent=2) + "\n")
            return fallback
        except Exception as e:
            fallback = {
                "expectations": [{"text": c, "passed": False, "evidence": f"Grading failed: {e}"} for c in criteria],
                "summary": {"passed": 0, "failed": len(criteria), "total": len(criteria), "pass_rate": 0.0},
            }
            (case_dir / "grading.json").write_text(json.dumps(fallback, indent=2) + "\n")
            return fallback

    # Should not reach here, but just in case
    return fallback  # type: ignore


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main() -> None:
    # Validate
    skill_md = SKILL_PATH / "SKILL.md"
    if not skill_md.exists():
        print(f"::error::SKILL.md not found at {skill_md}")
        sys.exit(1)

    cases = discover_evals(SKILL_PATH)
    if not cases:
        print(f"::error::No eval YAML files in {SKILL_PATH / 'evals'}")
        sys.exit(1)

    # Validate all cases before spending time/money on API calls
    validation_errors = validate_cases(cases)
    if validation_errors:
        print(f"::error::Found {len(validation_errors)} validation error(s) in eval cases:")
        for err in validation_errors:
            print(f"  ::error::{err}", file=sys.stderr)
        sys.exit(1)

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    skill_content = skill_md.read_text()

    print(f"Evaluating: {SKILL_NAME} ({len(cases)} cases, all validated)")

    # Execute
    exec_results = []
    for i, case in enumerate(cases):
        case_slug = case["name"].replace(" ", "-").lower()
        case_dir = WORKSPACE / case_slug
        print(f"::group::Execute [{i+1}/{len(cases)}]: {case['name']}")
        er = execute_case(case, skill_content, case_dir)
        exec_results.append(er)
        print(f"Status: {er['status']} | Time: {er['elapsed']}s | Tokens: {er['tokens']}")
        print("::endgroup::")

    # Grade
    gradings = []
    for i, (case, er) in enumerate(zip(cases, exec_results)):
        case_slug = case["name"].replace(" ", "-").lower()
        case_dir = WORKSPACE / case_slug
        print(f"::group::Grade [{i+1}/{len(cases)}]: {case['name']}")

        if er["status"] != "completed":
            fallback = {
                "expectations": [{"text": c, "passed": False, "evidence": f"Execution {er['status']}"} for c in case.get("criteria", [])],
                "summary": {"passed": 0, "failed": len(case.get("criteria", [])), "total": len(case.get("criteria", [])), "pass_rate": 0.0},
            }
            (case_dir / "grading.json").write_text(json.dumps(fallback, indent=2) + "\n")
            gradings.append(fallback)
            print(f"Skipped (execution {er['status']})")
        else:
            gr = grade_case(case, er, case_dir)
            gradings.append(gr)
            s = gr.get("summary", {})
            print(f"Result: {s.get('passed', 0)}/{s.get('total', 0)} passed")
        print("::endgroup::")

    # Aggregate
    total_passed = sum(g.get("summary", {}).get("passed", 0) for g in gradings)
    total_criteria = sum(g.get("summary", {}).get("total", 0) for g in gradings)
    total_time = sum(r.get("elapsed", 0) for r in exec_results)
    total_tokens = sum(r.get("tokens", 0) for r in exec_results)
    total_cost = sum(r.get("cost_usd", 0) for r in exec_results)
    pass_rate = (total_passed / total_criteria * 100) if total_criteria > 0 else 0

    # Write summary
    summary = {
        "skill_name": SKILL_NAME,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_cases": len(cases),
        "total_passed": total_passed,
        "total_criteria": total_criteria,
        "pass_rate": round(pass_rate, 1),
        "total_time": round(total_time, 1),
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 4),
        "results": [
            {
                "name": case["name"],
                "status": er["status"],
                "elapsed": er["elapsed"],
                "tokens": er["tokens"],
                "criteria_passed": gr.get("summary", {}).get("passed", 0),
                "criteria_total": gr.get("summary", {}).get("total", 0),
            }
            for case, er, gr in zip(cases, exec_results, gradings)
        ],
    }
    (WORKSPACE / "summary.json").write_text(json.dumps(summary, indent=2))

    # Set GitHub Actions outputs
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"pass_rate={pass_rate:.1f}\n")
            f.write(f"passed={total_passed}\n")
            f.write(f"total={total_criteria}\n")
            f.write(f"cases_run={len(cases)}\n")

    # Write step summary
    github_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if github_summary:
        with open(github_summary, "a") as f:
            status_emoji = "✅" if pass_rate >= PASS_THRESHOLD else "❌"
            f.write(f"## {status_emoji} Skill Eval: {SKILL_NAME}\n\n")
            f.write(f"**Pass rate: {total_passed}/{total_criteria} ({pass_rate:.1f}%)** | ")
            f.write(f"Threshold: {PASS_THRESHOLD:.0f}% | Time: {total_time:.1f}s | Tokens: {total_tokens:,} | Cost: ${total_cost:.4f}\n\n")
            f.write("| # | Case | Status | Criteria | Time | Tokens |\n")
            f.write("|---|------|--------|----------|------|--------|\n")
            for i, r in enumerate(summary["results"]):
                s = "PASS" if r["criteria_passed"] == r["criteria_total"] and r["status"] == "completed" else "FAIL"
                f.write(f"| {i+1} | {r['name']} | {s} | {r['criteria_passed']}/{r['criteria_total']} | {r['elapsed']}s | {r['tokens']:,} |\n")
            f.write("\n")

    # Print results table
    print(f"\n{'='*70}")
    print(f"Skill: {SKILL_NAME} | Pass rate: {total_passed}/{total_criteria} ({pass_rate:.1f}%)")
    print(f"{'='*70}")
    for i, r in enumerate(summary["results"]):
        s = "PASS" if r["criteria_passed"] == r["criteria_total"] and r["status"] == "completed" else "FAIL"
        print(f"  {i+1}. [{s}] {r['name']} — {r['criteria_passed']}/{r['criteria_total']} ({r['elapsed']}s, {r['tokens']:,} tokens)")
    print(f"\nTotal: {total_time:.1f}s | {total_tokens:,} tokens")


if __name__ == "__main__":
    main()
