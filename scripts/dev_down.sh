#!/usr/bin/env bash
cd "$(dirname "$0")/.."
for f in .data/*.pid; do [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null; rm -f "$f"; done
echo stopped
