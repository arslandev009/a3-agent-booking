"""Copy the shared `llm_client` package (found in the active environment) into ./vendor for the Docker build.

    python scripts\\vendor_llm_client.py

It also lists the third-party modules llm_client imports, so requirements.txt can be checked against them.
"""
from __future__ import annotations

import ast
import importlib.util
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def third_party_imports(pkg_dir: Path) -> set[str]:
    found: set[str] = set()
    for f in pkg_dir.rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return {m for m in found if m not in sys.stdlib_module_names and m != "llm_client"}


def main() -> None:
    spec = importlib.util.find_spec("llm_client")  # locate without importing (its own dependencies may be missing)
    if spec is None or not spec.origin:
        sys.exit("llm_client is not installed in this environment. Activate the ai-studio .venv first.")
    src = Path(spec.origin).resolve().parent
    dest = ROOT / "vendor" / "llm_client"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print(f"copied {src}\n    -> {dest}")
    print("third-party modules it imports:", ", ".join(sorted(third_party_imports(dest))) or "none")
    print("Make sure each is covered by requirements.txt (openai and python-dotenv already are).")


if __name__ == "__main__":
    main()
