#!/bin/bash
# Run london events digest using the project venv (Python 3.12, macOS)
cd "$(dirname "$0")"
venv/bin/python3 main.py "$@"
