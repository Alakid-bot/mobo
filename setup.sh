#!/bin/bash
set -euo pipefail

echo "=== 1812 Setup ==="

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set up .env if it doesn't exist
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example"
    echo "IMPORTANT: Edit .env and fill in your DISCORD_TOKEN and API key before running."
else
    echo ".env already exists, skipping."
fi

mkdir -p data

echo ""
echo "=== Setup Complete ==="
echo "Next steps:"
echo "1. Edit .env with your DISCORD_TOKEN and API key"
echo "2. Run: source .venv/bin/activate && python bot.py"
echo ""
echo "Or with Docker:"
echo "  docker compose up --build"
