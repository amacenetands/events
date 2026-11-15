#!/bin/bash
cd "$(dirname "$0")"
# qwen (local ollama) is always primary; haiku is the per-venue fallback when
# qwen errors or returns empty (see src/extract.py in-run upgrade).
export LLM_BACKEND=ollama
export CRAWL4_AI_BASE_DIRECTORY=/tmp
# Tee to variable so we can show summary without flooding context
OUT=$(venv/bin/python3 main.py --tag moscow "$@" 2>&1)
echo "$OUT" | grep -E "Fetched|events|Raw total|Saved|error|Error|failed" | grep -v "^$"
