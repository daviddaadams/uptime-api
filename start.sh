#!/bin/bash
# Quick start script for Uptime Monitor API

set -e

cd "$(dirname "$0")"

# Check if venv exists
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
fi

# Activate venv
source venv/bin/activate

# Check if dependencies are installed
if ! python -c "import fastapi" 2>/dev/null; then
    echo "📥 Installing dependencies..."
    pip install --only-binary=:all: -r requirements.txt
fi

# Start server
echo "🚀 Starting Uptime Monitor API..."
echo "📍 Server: http://localhost:8000"
echo "📚 API Docs: http://localhost:8000/docs"
echo ""
echo "Press Ctrl+C to stop"
echo ""

python main.py
