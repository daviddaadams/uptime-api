#!/bin/bash
cd /Users/davidadams/uptime-api

# Start the API server in background
source venv/bin/activate
uvicorn main:app --host 127.0.0.1 --port 8000 &
API_PID=$!
echo "API started with PID $API_PID"

# Start cloudflared tunnel
cloudflared tunnel --config .cloudflared/config.yml run &
TUNNEL_PID=$!
echo "Tunnel started with PID $TUNNEL_PID"

# Save PIDs
echo $API_PID > .api.pid
echo $TUNNEL_PID > .tunnel.pid

echo "OwlPulse running!"
echo "API: http://localhost:8000"
echo "Public: https://owlpulse.org"
echo "API endpoint: https://api.owlpulse.org"

wait
