from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import subprocess
import sys


ERROR_PATTERN = re.compile(r"^(.*?):\d+: error: (.*?)\s+\[([^\]]+)\]$")
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "mypy-baseline.json"


def parse_error_signatures(output: str) -> Counter[str]:
    signatures: Counter[str] = Counter()
    for line in output.splitlines():
        match = ERROR_PATTERN.match(line)
        if match is None:
            continue
        path, message, code = match.groups()
        signatures[f"{path}:{code}:{message}"] += 1
    return signatures


def find_new_errors(current: Counter[str], baseline: Counter[str]) -> Counter[str]:
    return current - baseline


def run_mypy() -> tuple[int, str]:
    command = [
        sys.executable,
        "-m",
        "mypy",
        "src/agent_bus",
        "--ignore-missing-imports",
        "--show-error-codes",
        "--no-pretty",
    ]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    return result.returncode, result.stdout + result.stderr


def load_baseline(path: Path) -> Counter[str]:
    payload = json.loads(path.read_text())
    if payload.get("format_version") != 1 or not isinstance(payload.get("signatures"), dict):
        raise ValueError(f"Unsupported mypy baseline format: {path}")
    return Counter({key: int(value) for key, value in payload["signatures"].items()})


def write_baseline(path: Path, signatures: Counter[str]) -> None:
    version = subprocess.run(
        [sys.executable, "-m", "mypy", "--version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    payload = {
        "format_version": 1,
        "generated_with": version,
        "total_errors": sum(signatures.values()),
        "signatures": dict(sorted(signatures.items())),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Reject new mypy errors while legacy debt is reduced")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--update", action="store_true", help="Replace the baseline with current errors")
    args = parser.parse_args()

    returncode, output = run_mypy()
    current = parse_error_signatures(output)
    if returncode not in (0, 1):
        sys.stderr.write(output)
        return returncode
    if returncode == 1 and not current:
        sys.stderr.write(output)
        return 2

    if args.update:
        write_baseline(args.baseline, current)
        print(f"Updated {args.baseline}: {sum(current.values())} errors")
        return 0

    baseline = load_baseline(args.baseline)
    new_errors = find_new_errors(current, baseline)
    if new_errors:
        print("New mypy errors exceed the committed baseline:", file=sys.stderr)
        for signature, count in sorted(new_errors.items()):
            print(f"  {count} x {signature}", file=sys.stderr)
        return 1

    print(
        f"mypy baseline respected: {sum(current.values())} current errors; "
        f"{sum(baseline.values())} baseline errors"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
