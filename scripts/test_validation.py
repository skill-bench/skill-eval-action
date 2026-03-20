#!/usr/bin/env python3
"""Local test for YAML loading, rubric normalization, and validation.

Usage:
    python scripts/test_validation.py <skills-dir>
    python scripts/test_validation.py /path/to/plugins/workable-company/skills

Runs without API calls — only tests discover + validate stages.
"""

import os
import sys
from pathlib import Path

# Set required env vars with defaults so eval.py can import
os.environ.setdefault("SKILL_NAME", "test")
os.environ.setdefault("SKILL_PATH", ".")
os.environ.setdefault("WORKSPACE", "/tmp/skill-eval-test")

from eval import _safe_yaml_load, discover_evals, validate_cases  # noqa: E402


def test_safe_yaml_load() -> int:
    """Test that _safe_yaml_load handles colons in plain scalars."""
    errors = 0

    # Should parse normally
    normal = _safe_yaml_load("key: value\nnested:\n  a: 1\n")
    assert normal == {"key": "value", "nested": {"a": 1}}, f"Normal parse failed: {normal}"

    # Should auto-fix colon in plain scalar
    broken = "grading:\n  rubric:\n    - id: R1\n      pass_if: Response covers categories: X, Y, Z\n"
    result = _safe_yaml_load(broken)
    pass_if = result["grading"]["rubric"][0]["pass_if"]
    if "categories: X, Y, Z" not in pass_if:
        print(f"  FAIL: colon auto-fix lost content: {pass_if}")
        errors += 1

    # Should auto-fix multiple lines
    multi = (
        "top:\n"
        "  a: value with colon: here\n"
        "  b: another one: too\n"
    )
    result = _safe_yaml_load(multi)
    if "colon: here" not in result["top"]["a"]:
        print(f"  FAIL: multi-fix line a: {result['top']['a']}")
        errors += 1
    if "one: too" not in result["top"]["b"]:
        print(f"  FAIL: multi-fix line b: {result['top']['b']}")
        errors += 1

    # Should preserve already-quoted values
    quoted = "key: \"value with colon: inside\"\n"
    result = _safe_yaml_load(quoted)
    if result["key"] != "value with colon: inside":
        print(f"  FAIL: quoted value changed: {result['key']}")
        errors += 1

    # Should preserve block scalars
    block = "key: |\n  line with colon: here\n  second line\n"
    result = _safe_yaml_load(block)
    if "colon: here" not in result["key"]:
        print(f"  FAIL: block scalar broken: {result['key']}")
        errors += 1

    return errors


def test_skills_dir(skills_dir: Path) -> int:
    """Discover and validate all skills in a directory."""
    errors = 0
    total_cases = 0
    total_criteria = 0

    if not skills_dir.is_dir():
        print(f"  ERROR: {skills_dir} is not a directory")
        return 1

    skill_dirs = sorted(
        d for d in skills_dir.iterdir()
        if d.is_dir() and (d / "evals").is_dir()
    )

    if not skill_dirs:
        print(f"  WARN: no skills with evals/ found in {skills_dir}")
        return 0

    for skill_dir in skill_dirs:
        os.environ["SKILL_NAME"] = skill_dir.name
        cases = discover_evals(skill_dir)
        if not cases:
            continue

        validation_errors = validate_cases(cases)
        n_criteria = sum(len(c.get("criteria", [])) for c in cases)
        total_cases += len(cases)
        total_criteria += n_criteria

        if validation_errors:
            print(f"  FAIL {skill_dir.name}: {len(validation_errors)} error(s)")
            for e in validation_errors:
                print(f"    x {e}")
            errors += len(validation_errors)
        else:
            print(f"  OK   {skill_dir.name}: {len(cases)} cases, {n_criteria} criteria")

    print(f"\n  Total: {total_cases} cases, {total_criteria} criteria")
    return errors


def main() -> None:
    print("=== _safe_yaml_load unit tests ===")
    unit_errors = test_safe_yaml_load()
    if unit_errors:
        print(f"FAIL: {unit_errors} unit test(s) failed\n")
    else:
        print("All unit tests passed\n")

    total_errors = unit_errors

    if len(sys.argv) > 1:
        skills_dir = Path(sys.argv[1])
        print(f"=== Validating skills in {skills_dir} ===")
        total_errors += test_skills_dir(skills_dir)

    if total_errors:
        print(f"\nFAILED: {total_errors} error(s)")
        sys.exit(1)
    else:
        print("\nAll tests passed.")


if __name__ == "__main__":
    main()
