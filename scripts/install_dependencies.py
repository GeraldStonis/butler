"""Retry build-time pip failures without caching truncated package-index data."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_with_retry(command: list[str], *, label: str, attempts: int = 3) -> int:
    for attempt in range(1, attempts + 1):
        print(f"{label}: attempt {attempt}/{attempts}", flush=True)
        result = subprocess.run(command, check=False)
        if result.returncode == 0:
            return 0
        if attempt < attempts:
            delay = 5 * attempt
            print(f"{label} failed; retrying in {delay} seconds.", flush=True)
            time.sleep(delay)
    print(f"{label} failed after {attempts} attempts.", file=sys.stderr, flush=True)
    return result.returncode


def main() -> int:
    common = [
        sys.executable, "-m", "pip", "install", "--no-input",
        "--no-cache-dir", "--disable-pip-version-check",
        "--retries", "10", "--timeout", "60",
        "--index-url", "https://pypi.org/simple",
    ]
    code = run_with_retry(common + ["--upgrade", "pip>=25.1"], label="Upgrade pip")
    if code:
        return code
    # Connection retries alone do not retry malformed package-index JSON.
    code = run_with_retry(common + ["--resume-retries", "10", "-r", str(ROOT / "requirements.txt")],
                          label="Install bot dependencies")
    if code:
        return code
    return run_with_retry([sys.executable, "-m", "pip", "check"], label="Check dependencies", attempts=1)


if __name__ == "__main__":
    raise SystemExit(main())
