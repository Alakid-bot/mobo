#!/bin/bash
set -euo pipefail

echo "=== Publishing 1812 to GitHub ==="

git init
git add .
git commit -m "initial commit"
gh repo create CryptoJones/1812 --public --source=. --push

echo ""
echo "=== Done ==="
echo "Repo live at: https://github.com/CryptoJones/1812"
