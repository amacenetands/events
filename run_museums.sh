#!/bin/bash
cd "$(dirname "$0")"
export CRAWL4_AI_BASE_DIRECTORY=/tmp
# Tee to file so we can show summary without flooding context
OUT=$(venv/bin/python3 main.py --museums-only "$@" 2>&1)
echo "$OUT" | grep -E "Fetched|events|Raw total|Final|Saved|error|Error|failed" | grep -v "^$"
