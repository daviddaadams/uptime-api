# Uptime Monitor API - MVP

A simple, fast uptime monitoring service built with FastAPI. Monitor your websites and get webhook alerts when they go down.

## Features

✅ **REST API** for managing monitors  
✅ **Background checking** (1 min for Pro, 5 min for Free)  
✅ **Response time tracking** with uptime percentage  
✅ **Webhook alerts** on status changes  
✅ **API key authentication**  
✅ **SQLite database** (zero configuration)
✅ **Public status pages** (JSON + rendered HTML)
✅ **Status-page branding** (title, colors, logo)
✅ **Status subscriptions** (`POST /status/{slug}/subscribe`)

## Quick Start

### 1. Install Dependencies

```bash
cd /Users/davidadams/uptime-api
python3 -m venv venv
source venv/bin/activate

# For Python 3.14+, use pre-built wheels:
pip install --only-binary=:all: -r requirements.txt

# Or for older Python versions:
# pip install -r requirements.txt
```

### 2. Run the Server

```bash
python main.py
```

Server runs on `http://localhost:8000`

API docs available at `http://localhost:8000/docs`

### 3. Sign Up And Create Your First Monitor

First create an account and capture the returned `api_key`:

```bash
curl -X POST http://localhost:8000/signup \
  -H "Content-Type: application/json" \
  -d '{
    "email": "you@example.com",
    "url": "https://example.com",
    "name": "My Website",
    "plan": "pro"
  }'
```

Then create monitors with that API key:

```bash
curl -X POST http://localhost:8000/monitors \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY_HERE" \
  -d '{
    "url": "https://example.com",
    "name": "My Website",
    "plan": "pro",
    "webhook_url": "https://your-webhook-endpoint.com/alerts"
  }'
```

## API Reference

### Create Monitor
```http
POST /monitors
Content-Type: application/json

{
  "url": "https://example.com",
  "name": "My Site",
  "plan": "free",  // or "pro"
  "webhook_url": "https://hooks.example.com/alerts"  // optional
}
```

**Response:**
```json
{
  "id": 1,
  "url": "https://example.com",
  "name": "My Site",
  "plan": "free",
  "api_key": "YOUR_API_KEY_HERE",
  "status": "unknown",
  "uptime_percentage": 0.0,
  "total_checks": 0
}
```

### List Monitors
```http
GET /monitors
X-API-Key: YOUR_API_KEY
```

### Get Monitor Details
```http
GET /monitors/{id}
X-API-Key: YOUR_API_KEY
```

### Delete Monitor
```http
DELETE /monitors/{id}
X-API-Key: YOUR_API_KEY
```

### Get Check History
```http
GET /monitors/{id}/checks?limit=100
X-API-Key: YOUR_API_KEY
```

### Trigger Manual Check
```http
POST /monitors/{id}/check
X-API-Key: YOUR_API_KEY
```

### Status Pages (Admin CRUD + Public Rendering)

Create a status page:

```bash
curl -X POST http://localhost:8000/status-pages \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{
    "title": "Acme API Status",
    "slug": "acme-api",
    "description": "Real-time health for Acme APIs",
    "theme_color": "#0EA5E9",
    "accent_color": "#38BDF8",
    "background_color": "#0A0E1A",
    "text_color": "#E2E8F0",
    "logo_url": "https://cdn.example.com/logo.png",
    "monitor_ids": [1, 2, 3]
  }'
```

Update branding fields:

```bash
curl -X PUT http://localhost:8000/status-pages/1 \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{
    "title": "Acme Platform Status",
    "accent_color": "#22D3EE",
    "background_color": "#020617",
    "text_color": "#F1F5F9",
    "logo_url": "https://cdn.example.com/new-logo.png"
  }'
```

Public JSON endpoint (kept backward-compatible):

```bash
curl http://localhost:8000/status-pages/acme-api/public
```

Public rendered HTML endpoint:

```bash
curl http://localhost:8000/status/acme-api
```

Subscribe an email for updates:

```bash
curl -X POST http://localhost:8000/status/acme-api/subscribe \
  -H "Content-Type: application/json" \
  -d '{"email":"ops@example.com"}'
```

## Plans

| Plan | Check Interval |
|------|----------------|
| Free | 5 minutes      |
| Pro  | 1 minute       |

## Webhook Format

When a monitor's status changes, the webhook receives:

```json
{
  "monitor_id": 1,
  "name": "My Website",
  "url": "https://example.com",
  "status": "down",
  "previous_status": "up",
  "timestamp": 1707504000,
  "status_code": null,
  "response_time_ms": 10234.5
}
```

## Authentication

Authenticated monitor operations require the `X-API-Key` header, including `POST /monitors`:

```bash
curl -H "X-API-Key: your-api-key-here" http://localhost:8000/monitors
```

Get an API key from `POST /signup`, then include it on monitor-management requests.

## How It Works

1. **Background Scheduler** runs every 30 seconds
2. Checks which monitors need checking based on their plan
3. Makes HTTP requests to monitored URLs (10s timeout)
4. Records success/failure, status code, and response time
5. Sends webhooks if status changed (up ↔ down)
6. Calculates uptime percentage from all checks

## Database

SQLite database (`uptime.db`) with tables including:

- `monitors` - Your monitored URLs and settings
- `checks` - Historical check results
- `alert_events` - Monitor status transitions
- `status_pages` - Status-page metadata and branding config
- `status_page_monitors` - Monitor membership per status page
- `status_page_subscribers` - Subscription emails per status page

## Production Deployment

### Environment Variables (optional)
```bash
export DATABASE="/path/to/uptime.db"
export PORT=8000
```

### Run with uvicorn
```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

### Docker (coming soon)

## Development

```bash
# Install in development mode
pip install -r requirements.txt

# Run with auto-reload
uvicorn main:app --reload

# View logs
tail -f uvicorn.log
```

## Testing

```bash
# Health check
curl http://localhost:8000/

# Create a test monitor
curl -X POST http://localhost:8000/monitors \
  -H "Content-Type: application/json" \
  -d '{"url": "https://httpbin.org/status/200", "name": "Test"}'

# Trigger immediate check
curl -X POST http://localhost:8000/monitors/1/check \
  -H "X-API-Key: YOUR_KEY"

# View results
curl http://localhost:8000/monitors/1/checks \
  -H "X-API-Key: YOUR_KEY"
```

## Limitations (MVP)

- Single API key per monitor (not multi-user)
- No user accounts or dashboard
- No email alerts (webhooks only)
- No SSL certificate monitoring
- No custom check intervals
- Basic auth (no OAuth/JWT)

## Roadmap

- [ ] Multi-user support with accounts
- [ ] Web dashboard
- [ ] Email/SMS alerts
- [ ] Custom check intervals
- [ ] SSL certificate expiry monitoring
- [ ] Incident history and reporting
- [x] Status page generation
- [ ] Docker support

## License

MIT

## Support

Built as a Day 1 MVP. Iterate, improve, ship! 🚀
