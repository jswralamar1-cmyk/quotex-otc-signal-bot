#!/bin/bash
set -e

echo "=== Installing Python dependencies ==="
pip install pyquotex@git+https://github.com/cleitonleonel/pyquotex.git requests playwright

echo "=== Installing Playwright Chromium ==="
playwright install chromium
playwright install-deps chromium

echo "=== Build complete ==="
