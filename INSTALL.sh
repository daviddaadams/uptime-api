#!/bin/bash
# One-line installer for Uptime Monitor API

set -e

echo "🚀 Installing Uptime Monitor API..."
echo ""

# Check Python version
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is required but not found"
    exit 1
fi

PYTHON_VERSION=$(python3 --version | cut -d' ' -f2)
echo "✓ Found Python $PYTHON_VERSION"

# Create venv if doesn't exist
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
fi

# Activate
source venv/bin/activate

# Install dependencies
echo "📥 Installing dependencies..."
if [[ "$PYTHON_VERSION" == 3.14.* ]]; then
    pip install --only-binary=:all: -q -r requirements.txt
else
    pip install -q -r requirements.txt
fi

echo ""
echo "✅ Installation complete!"
echo ""
echo "To start the server:"
echo "  ./start.sh"
echo ""
echo "Or manually:"
echo "  source venv/bin/activate"
echo "  python main.py"
echo ""
echo "📚 Docs: http://localhost:8000/docs"
