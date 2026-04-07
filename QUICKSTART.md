# ⚡ Quick Start - 60 Second Setup

## Install & Run

```bash
cd /Users/davidadams/uptime-api
./start.sh
```

That's it! Server running on http://localhost:8000

## Create Your First Monitor

```bash
# Create a monitor
curl -X POST http://localhost:8000/monitors \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "name": "My Website",
    "plan": "pro"
  }'
```

You'll get back an `api_key` - **save it!**

## Check Your Monitors

```bash
curl http://localhost:8000/monitors \
  -H "X-API-Key: YOUR_API_KEY_HERE"
```

## View Interactive Docs

Open http://localhost:8000/docs in your browser for full API documentation.

## What Happens Next?

The background scheduler will:
- Check your Pro monitors every 1 minute
- Check your Free monitors every 5 minutes
- Track response times and uptime percentage
- Send webhooks when sites go down/up

## Need Help?

- **API Docs:** http://localhost:8000/docs
- **Full Guide:** See README.md
- **Testing:** See TESTING.md
- **Production:** See DEPLOYMENT.md

---

Built fast. Ships fast. Iterate faster. 🚀
