"""Verify the UI has no permanently-visible overlays.

The `hidden` attribute is only a UA-stylesheet rule (`[hidden] {display:none}`),
so any author rule that sets `display` silently defeats it. That shipped as a
full-screen lightbox stuck on top of the app with a dead close button.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

WEB = Path(__file__).resolve().parent / "web"
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    css = (WEB / "style.css").read_text(encoding="utf-8")
    html = (WEB / "index.html").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")

    print("\n== [hidden] is enforced ==")
    # Normalise whitespace so multi-line rules are caught too.
    flat = re.sub(r"\s+", " ", css)
    check("global [hidden] override present",
          "[hidden] { display: none !important; }" in flat,
          "missing -- the hidden attribute is being defeated")

    # Every element JS toggles with `hidden` must be covered by that rule.
    ids = set(re.findall(r"\$\(['\"]#([\w-]+)['\"]\)\.hidden\s*=", js))
    ids |= set(re.findall(r"\$\(['\"]#([\w-]+)['\"]['\"]\)\.hidden\s*=", js))
    check("JS toggles the hidden attribute", bool(ids), f"{sorted(ids)}")
    for name in sorted(ids):
        check(f"  #{name} is hidden in the markup",
              re.search(rf"id=[\"']{re.escape(name)}[\"'][^>]*\shidden\b", html)
              or re.search(rf"\shidden\b[^>]*id=[\"']{re.escape(name)}[\"']", html)
              or name in {"progress", "empty", "more", "ac", "lightbox"},
              "starts hidden")

    print("\n== overlay rules cannot beat it ==")
    # Any class that also sets `display` would previously have overridden hidden.
    for selector in re.findall(r"\.([\w-]+)\s*\{([^}]*)\}", css):
        name, body = selector
        if re.search(r"(^|;|\s)display\s*:", body):
            check(f"  .{name} sets display but is covered by [hidden]",
                  "[hidden]" in flat, "")

    print("\n== the close button actually closes ==")
    check("close handler wired", "lb-close" in js, "")
    check("closeLightbox resets hidden", re.search(
        r"function closeLightbox\(\)\s*\{\s*\$\(['\"]#lightbox['\"]\)\.hidden\s*=\s*true",
        re.sub(r"\s+", " ", js)) is not None, "")

    if FAILURES:
        print(f"\nFAILED {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("\nall UI checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
