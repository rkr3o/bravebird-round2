#!/bin/bash
set -e

echo "Setting up GitHub Automation Bot..."

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 -m playwright install chromium

echo ""
echo "Setup complete. Next steps:"
echo "  1. cp .env.example .env"
echo "  2. Fill in GITHUB_USERNAME, GITHUB_PASSWORD, GITHUB_TOTP_SECRET"
echo "  3. python3 bot.py"
