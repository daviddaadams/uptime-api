# Testing Guide

## Quick Test

Run the automated test script:

```bash
# Start server in one terminal
python main.py

# In another terminal, run tests
./test_api.sh
```

## Manual Testing Workflow

### 1. Start the Server

```bash
source venv/bin/activate
python main.py
```

Server runs on http://localhost:8000
API docs at http://localhost:8000/docs

### 2. Create a Monitor

```bash
curl -X POST http://localhost:8000/monitors \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "name": "My Website",
    "plan": "pro",
    "webhook_url": "http://localhost:5000/webhook"
  }' | jq
```

Save the `api_key` from the response!

### 3. Test Webhook Alerts

In a separate terminal, run the webhook receiver:

```bash
# First install Flask: pip install flask
python webhook_example.py
```

Then create a monitor that points to a site you can control (or use httpbin):

```bash
# Monitor a URL that will fail
curl -X POST http://localhost:8000/monitors \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://httpbin.org/status/500",
    "name": "Failing Site",
    "plan": "pro",
    "webhook_url": "http://localhost:5000/webhook"
  }' | jq
```

Wait for the background checker to detect it's down and send a webhook.

### 4. Test Different Scenarios

**Test uptime tracking:**
```bash
API_KEY="your-api-key-here"

# Trigger multiple checks
for i in {1..5}; do
  curl -X POST http://localhost:8000/monitors/1/check \
    -H "X-API-Key: $API_KEY"
  sleep 2
done

# View uptime percentage
curl http://localhost:8000/monitors/1 \
  -H "X-API-Key: $API_KEY" | jq '.uptime_percentage'
```

**Test response time tracking:**
```bash
# Monitor slow endpoint
curl -X POST http://localhost:8000/monitors \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://httpbin.org/delay/3",
    "name": "Slow Site",
    "plan": "free"
  }' | jq

# Check response times
curl http://localhost:8000/monitors/2/checks \
  -H "X-API-Key: $API_KEY" | jq '.[].response_time'
```

**Test plan differences:**
```bash
# Create free plan monitor (checks every 5 min)
curl -X POST http://localhost:8000/monitors \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "name": "Free", "plan": "free"}' | jq

# Create pro plan monitor (checks every 1 min)
curl -X POST http://localhost:8000/monitors \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "name": "Pro", "plan": "pro"}' | jq
```

## Test with Real Sites

```bash
# Test with reliable sites
curl -X POST http://localhost:8000/monitors \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.google.com",
    "name": "Google",
    "plan": "pro"
  }' | jq

curl -X POST http://localhost:8000/monitors \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.github.com",
    "name": "GitHub",
    "plan": "pro"
  }' | jq
```

## Verify Background Checks

Watch the logs to see the background scheduler in action:

```bash
tail -f server.log
```

You should see checks happening:
- Pro monitors: every ~1 minute
- Free monitors: every ~5 minutes

## Database Inspection

```bash
sqlite3 uptime.db

# View all monitors
SELECT * FROM monitors;

# View recent checks
SELECT m.name, c.timestamp, c.success, c.response_time 
FROM checks c 
JOIN monitors m ON c.monitor_id = m.id 
ORDER BY c.timestamp DESC 
LIMIT 10;

# Calculate uptime by monitor
SELECT 
  m.name,
  m.total_checks,
  m.successful_checks,
  ROUND(100.0 * m.successful_checks / m.total_checks, 2) as uptime_pct
FROM monitors m
WHERE m.total_checks > 0;
```

## Performance Testing

Test with multiple monitors:

```bash
# Create 10 monitors
for i in {1..10}; do
  curl -X POST http://localhost:8000/monitors \
    -H "Content-Type: application/json" \
    -d "{\"url\": \"https://httpbin.org/delay/$((RANDOM % 3))\", \"name\": \"Test $i\", \"plan\": \"pro\"}"
done

# Watch them all get checked
watch -n 1 "curl -s http://localhost:8000/monitors -H 'X-API-Key: YOUR_KEY' | jq '.[] | {name, status, last_checked}'"
```

## Cleanup

```bash
# Stop server
pkill -f "python main.py"

# Reset database
rm uptime.db

# Restart fresh
python main.py
```
