#!/bin/bash
# PC Peak Tax Foreclosure Intelligence Platform
# Starts backend API + opens dashboard

echo ""
echo "╔═══════════════════════════════════════════════════════╗"
echo "║   PC Peak Tax Foreclosure Intelligence Platform       ║"
echo "╚═══════════════════════════════════════════════════════╝"
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 not found. Install from python.org"
    exit 1
fi

# Install deps if needed
if ! python3 -c "import fastapi" 2>/dev/null; then
    echo "Installing dependencies..."
    pip install -r requirements.txt
    playwright install chromium
fi

# Create data directories
mkdir -p data/db data/pdfs

# Check for API key
if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "⚠  ANTHROPIC_API_KEY not set."
    echo "   Export it: export ANTHROPIC_API_KEY=sk-ant-..."
    echo "   Or add it to .env file"
fi

# Load .env if it exists
if [ -f .env ]; then
    export $(cat .env | grep -v '#' | xargs)
fi

echo "Starting backend API on http://localhost:8000..."
cd backend && python3 main.py &
BACKEND_PID=$!
cd ..

sleep 2

echo "Opening dashboard..."
open http://localhost:8080 2>/dev/null || xdg-open http://localhost:8080 2>/dev/null

# Serve the frontend
echo "Starting dashboard on http://localhost:8080..."
cd frontend && python3 -m http.server 8080 &
FRONTEND_PID=$!
cd ..

echo ""
echo "Platform running:"
echo "  Dashboard:  http://localhost:8080"
echo "  API:        http://localhost:8000"
echo "  API Docs:   http://localhost:8000/docs"
echo ""
echo "To run the agent manually:"
echo "  python3 agent/agent.py"
echo "  python3 agent/agent.py --case TX-26-00009"
echo "  python3 agent/agent.py --schedule"
echo ""
echo "Press Ctrl+C to stop everything"

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; echo 'Platform stopped.'" INT
wait
