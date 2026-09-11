#!/bin/bash
#
# LAMA Backend Startup Script
#
# This script starts the backend FastAPI service with all dependencies
#

set -e

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🚀 Starting LAMA Backend Service"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Change to backend directory
cd "$(dirname "$0")"

# Check if .env exists
if [ ! -f ".env" ]; then
    echo "⚠️  Warning: No .env file found. Creating from example..."
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "✅ Created .env from .env.example"
    else
        echo "⚠️  No .env.example found either. Backend may not work properly."
        echo "   Please create .env with required environment variables."
    fi
fi

# Check if MongoDB is running (for local dev)
echo ""
echo "📊 Checking MongoDB..."
if command -v mongod &> /dev/null; then
    if pgrep -x "mongod" > /dev/null; then
        echo "✅ MongoDB is running"
    else
        echo "⚠️  MongoDB is not running. Starting it..."
        # Uncomment if you want auto-start
        # mongod --config /usr/local/etc/mongod.conf --fork
        echo "   Please start MongoDB manually: mongod --config /usr/local/etc/mongod.conf"
    fi
else
    echo "⚠️  MongoDB not found. Using remote MongoDB if configured in .env"
fi

# Check Python version
echo ""
echo "🐍 Python environment:"
python3 --version
echo "   Location: $(which python3)"

# Check if uvicorn is installed
echo ""
echo "📦 Checking dependencies..."
if ! python3 -c "import uvicorn" 2>/dev/null; then
    echo "⚠️  uvicorn not found. Installing dependencies..."
    echo "   This may take a minute..."
    
    # Try with --user flag to avoid system package conflicts
    python3 -m pip install -r requirements.txt --user --quiet
    
    if [ $? -eq 0 ]; then
        echo "✅ Dependencies installed"
    else
        echo "❌ Failed to install dependencies"
        echo "   Please run manually: python3 -m pip install -r requirements.txt --user"
        exit 1
    fi
else
    echo "✅ Dependencies OK"
fi

# Start the server
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🎯 Starting FastAPI server on http://localhost:8000"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Available endpoints:"
echo "  Health:     http://localhost:8000/api/health"
echo "  API Docs:   http://localhost:8000/docs"
echo "  Auth Login: http://localhost:8000/api/auth/login"
echo ""
echo "Press Ctrl+C to stop the server"
echo ""

# Start uvicorn with reload for development
python3 -m uvicorn server:app --reload --host 0.0.0.0 --port 8000
