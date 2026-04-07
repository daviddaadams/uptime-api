#!/bin/bash
# Quick API test script

set -e

BASE_URL="${BASE_URL:-http://localhost:8000}"

echo "🧪 Testing Uptime Monitor API"
echo "================================"
echo ""

# Health check
echo "✓ Health check..."
curl -sf "$BASE_URL/" | python3 -m json.tool
echo ""

# Create monitor
echo "✓ Creating test monitor..."
RESPONSE=$(curl -sf -X POST "$BASE_URL/monitors" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://httpbin.org/delay/1",
    "name": "Test Site",
    "plan": "pro"
  }')

API_KEY=$(echo "$RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin)['api_key'])")
echo "API Key: $API_KEY"
echo ""

# Trigger check
echo "✓ Triggering check..."
curl -sf -X POST "$BASE_URL/monitors/1/check" \
  -H "X-API-Key: $API_KEY" > /dev/null
sleep 2

# View results
echo "✓ Viewing results..."
curl -sf "$BASE_URL/monitors/1" \
  -H "X-API-Key: $API_KEY" | python3 -m json.tool
echo ""

echo "✅ All tests passed!"
echo ""
echo "API Key for further testing: $API_KEY"
