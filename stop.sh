#!/bin/bash
cd /Users/davidadams/uptime-api
[ -f .api.pid ] && kill $(cat .api.pid) 2>/dev/null && rm .api.pid
[ -f .tunnel.pid ] && kill $(cat .tunnel.pid) 2>/dev/null && rm .tunnel.pid
pkill -f "uvicorn main:app.*8000" 2>/dev/null
pkill -f "cloudflared.*owlpulse" 2>/dev/null
echo "OwlPulse stopped"
