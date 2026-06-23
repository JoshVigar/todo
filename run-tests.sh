#!/usr/bin/env bash
# Watch mode: reruns tests on any .py file change.
# Usage: ./run-tests.sh              # watch mode
#        ./run-tests.sh --once       # single run
#        ./run-tests.sh -k "email"   # single run with pytest args
set -euo pipefail
cd "$(dirname "$0")"

if [[ $# -eq 0 ]]; then
    echo "Watching for changes... (Ctrl+C to stop)"
    while true; do
        find . -maxdepth 1 -name '*.py' \
            | entr -d python3 -m pytest -q --tb=short
    done
elif [[ "$1" == "--once" ]]; then
    shift
    python3 -m pytest -q --tb=short "$@"
else
    python3 -m pytest -q --tb=short "$@"
fi
