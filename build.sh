#!/bin/bash
set -e

echo "=== Installing Python dependencies ==="
pip install -r requirements.txt

echo "=== Installing Playwright ==="
pip install playwright

echo "=== Installing Chromium browser ==="
playwright install chromium
playwright install-deps chromium || true

echo "=== Build complete ==="
