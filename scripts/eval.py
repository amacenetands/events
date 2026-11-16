"""Gold-set evaluation + drift check (pipeline v3 §1.7). See src/eval/cli.py for options.

  venv/bin/python scripts/eval.py [--only SLUG] [--json out.json] [--baseline [tests/gold/baseline.json]]
  venv/bin/python scripts/eval.py --update-baseline
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.eval.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
