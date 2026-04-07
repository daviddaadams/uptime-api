# Uptime Monitor API - Project Overview

## 🎯 Mission

Build a simple, fast, self-hosted uptime monitoring service that just works.

## ✅ MVP Deliverables (COMPLETED)

- [x] FastAPI backend with REST endpoints
- [x] SQLite database (zero configuration)
- [x] POST /monitors - Add new monitors
- [x] GET /monitors - List all monitors
- [x] DELETE /monitors/{id} - Remove monitors
- [x] Background scheduler (APScheduler)
- [x] Check intervals: 1 min (Pro), 5 min (Free)
- [x] Response time tracking
- [x] Uptime percentage calculation
- [x] Webhook alerts on status changes
- [x] API key authentication
- [x] Manual check trigger endpoint
- [x] Check history endpoint
- [x] Complete documentation

## 📂 Project Structure

```
uptime-api/
├── main.py              # Core application (14KB, ~500 lines)
├── requirements.txt     # Python dependencies
├── README.md            # User-facing docs
├── TESTING.md           # Test workflows
├── DEPLOYMENT.md        # Production deployment
├── PROJECT.md           # This file
├── start.sh             # Quick start script
├── test_api.sh          # Automated tests
├── webhook_example.py   # Webhook receiver demo
├── .gitignore
├── uptime.db            # SQLite database (auto-created)
└── venv/                # Python virtual environment
```

## 🏗️ Architecture

### Core Components

1. **FastAPI Application** (`main.py`)
   - RESTful API endpoints
   - Pydantic models for validation
   - Dependency injection for auth

2. **SQLite Database**
   - `monitors` table: Configuration and stats
   - `checks` table: Historical check data
   - Indexes for performance

3. **Background Scheduler** (APScheduler)
   - Runs every 30 seconds
   - Checks monitors based on plan interval
   - Pro: 1 minute, Free: 5 minutes

4. **HTTP Client** (httpx)
   - Async requests to monitored URLs
   - 10-second timeout
   - Follows redirects

5. **Webhook System**
   - Fires on status changes (up ↔ down)
   - JSON payload with details
   - Fire-and-forget (no retries in MVP)

### Data Flow

```
1. User creates monitor (POST /monitors)
   └─> Generate API key
   └─> Store in database
   └─> Return monitor details

2. Background scheduler runs
   └─> Query monitors due for check
   └─> Perform HTTP request
   └─> Record result in database
   └─> Send webhook if status changed

3. User queries stats (GET /monitors/{id})
   └─> Fetch from database
   └─> Calculate uptime percentage
   └─> Return aggregated data
```

## 🎨 Design Decisions

### Why These Technologies?

- **FastAPI**: Modern, fast, automatic OpenAPI docs
- **SQLite**: Zero config, perfect for self-hosted
- **httpx**: Async HTTP with clean API
- **APScheduler**: Simple background tasks without Celery
- **Plain sqlite3**: No ORM overhead for simple queries

### Trade-offs

| Choice | Pro | Con |
|--------|-----|-----|
| SQLite | Zero setup, portable | Not for massive scale |
| API key per monitor | Simple auth | Not multi-user |
| In-process scheduler | No external dependencies | Single instance only |
| No email alerts | Webhooks are flexible | Requires external service |

## 📊 Current Limitations

1. **Single-user architecture** - Each monitor has its own API key, no user accounts
2. **No retry logic** - If webhook fails, notification is lost
3. **No email/SMS** - Only webhooks (by design)
4. **Fixed check intervals** - Can't customize per monitor
5. **Single scheduler** - Can't run multiple API instances
6. **No SSL monitoring** - Only checks HTTP response
7. **No geographic distribution** - Checks from single location
8. **Basic auth** - No OAuth, JWT, or RBAC

## 🚀 Roadmap

### Phase 2: Multi-User (1-2 weeks)

- [ ] User accounts and authentication
- [ ] JWT tokens instead of API keys
- [ ] User dashboard (React/Vue frontend)
- [ ] Multiple monitors per user
- [ ] User settings and preferences

### Phase 3: Enhanced Monitoring (1 week)

- [ ] Custom check intervals
- [ ] SSL certificate expiry monitoring
- [ ] HTTP header checks (specific status codes)
- [ ] Keyword/content monitoring
- [ ] Multiple alert channels (email, SMS, Slack)
- [ ] Alert frequency limits (no spam)

### Phase 4: Reliability (1 week)

- [ ] Webhook retry with exponential backoff
- [ ] Dead-letter queue for failed notifications
- [ ] Rate limiting per user
- [ ] Distributed checking (Redis-backed)
- [ ] PostgreSQL support for scale
- [ ] API caching layer

### Phase 5: Analytics (1-2 weeks)

- [ ] Incident timeline
- [ ] Historical graphs (response time, uptime)
- [ ] Monthly/weekly reports
- [ ] SLA tracking
- [ ] Downtime patterns analysis
- [ ] Export data (CSV, JSON)

### Phase 6: Status Pages (2 weeks)

- [ ] Public status page generator
- [ ] Custom domains
- [ ] Incident posting
- [ ] Maintenance schedules
- [ ] Subscriber notifications
- [ ] Embeddable widgets

### Phase 7: Integrations (ongoing)

- [ ] Slack app
- [ ] Discord bot
- [ ] PagerDuty integration
- [ ] Datadog/Grafana export
- [ ] GitHub Actions monitoring
- [ ] Zapier/Make.com webhooks

## 📈 Metrics to Track

As the project grows, monitor:

- **Performance**
  - API response time (p50, p95, p99)
  - Check completion time
  - Database query time
  - Background job lag

- **Usage**
  - Active monitors count
  - Checks per minute
  - Webhook delivery rate
  - API requests per endpoint

- **Reliability**
  - Uptime of monitoring service itself
  - Failed webhook delivery rate
  - Database errors
  - Background scheduler health

## 🧪 Testing Strategy

### Current (MVP)

- Manual testing with `test_api.sh`
- Basic smoke tests
- Example webhook receiver

### Future

- [ ] Unit tests (pytest)
- [ ] Integration tests
- [ ] Load testing (Locust)
- [ ] E2E tests (Playwright)
- [ ] CI/CD pipeline (GitHub Actions)

## 💰 Monetization Ideas

If turning this into a service:

### Free Tier
- 10 monitors
- 5-minute checks
- 1,000 checks/month
- Community support

### Pro Tier ($9/month)
- 50 monitors
- 1-minute checks
- Unlimited checks
- Email support
- SSL monitoring

### Business Tier ($49/month)
- Unlimited monitors
- 30-second checks
- Custom intervals
- Phone support
- SLA guarantee
- Team features

## 🔒 Security Enhancements

For production use:

1. **API Security**
   - Rate limiting (per API key)
   - Request size limits
   - CORS configuration
   - SQL injection prevention (parameterized queries ✓)

2. **Authentication**
   - Bcrypt for passwords
   - JWT with expiry
   - API key rotation
   - 2FA support

3. **Infrastructure**
   - HTTPS only
   - Firewall rules
   - Database encryption at rest
   - Secrets management (Vault, AWS Secrets)

4. **Monitoring**
   - Failed auth attempts
   - Unusual traffic patterns
   - Database access logs
   - Webhook abuse detection

## 📝 Documentation Status

- [x] README.md - Getting started guide
- [x] TESTING.md - Testing workflows
- [x] DEPLOYMENT.md - Production deployment
- [x] PROJECT.md - This overview
- [x] Inline code comments
- [x] API docs (auto-generated by FastAPI)
- [ ] Architecture diagrams
- [ ] Video walkthrough
- [ ] Blog post / announcement

## 🤝 Contributing

Currently a solo MVP project. Future:

- [ ] CONTRIBUTING.md
- [ ] Code of conduct
- [ ] Issue templates
- [ ] PR guidelines
- [ ] Development setup guide

## 📜 License

MIT License - Use freely, build upon it, make it your own.

## 🙏 Acknowledgments

Built with:
- FastAPI by Sebastián Ramírez
- APScheduler by Alex Grönholm
- httpx by Tom Christie
- Pydantic by Samuel Colvin

---

**Status:** ✅ MVP Complete  
**Next:** Deploy, test with real users, iterate based on feedback  
**Goal:** Ship fast, learn faster 🚀
