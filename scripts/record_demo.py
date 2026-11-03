"""Runs scripts/demo.py once and saves its real output, line by line, for the site's replay.

The result is site/demo-cast.js. It keeps every line exactly as the demo printed it, with the
time each one appeared. The site and the README GIF replay it, slowed down so it can be read.

Run it with: python3 scripts/record_demo.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "site" / "demo-cast.js"


def main() -> int:
    started = time.time()
    process = subprocess.Popen(
        [sys.executable, "-u", "-W", "ignore::ResourceWarning", str(ROOT / "scripts" / "demo.py")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=ROOT,
    )
    lines = []
    for line in process.stdout:
        lines.append([round(time.time() - started, 3), line.rstrip("\n")])
    if process.wait() != 0:
        print("\n".join(text for _, text in lines), file=sys.stderr)
        return 1
    cast = {"recorded": time.strftime("%Y-%m-%d", time.gmtime()), "seconds": round(time.time() - started, 1), "lines": lines}
    OUT.write_text("// A real run of scripts/demo.py, made by scripts/record_demo.py. Don't edit by hand.\n"
                   f"window.DEMO_CAST = {json.dumps(cast, indent=1)};\n")
    print(f"Saved {len(lines)} lines from a {cast['seconds']} second run to {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
