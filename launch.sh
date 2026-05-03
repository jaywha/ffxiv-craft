#!/bin/bash
echo ""
echo " ============================================"
echo "  FFXIV Craft Planner"
echo " ============================================"
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
    echo " [ERROR] python3 not found. Install Python 3.10+ from python.org"
    exit 1
fi

# Install deps if needed
if ! python3 -c "import flask" &>/dev/null; then
    echo " Installing dependencies..."
    pip3 install -r requirements.txt --quiet
fi

echo " Starting server at http://localhost:5000"
echo " Press Ctrl+C to stop."
echo ""

# Open browser
sleep 1.5 && (open http://localhost:5000 2>/dev/null || xdg-open http://localhost:5000 2>/dev/null) &

python3 app.py
