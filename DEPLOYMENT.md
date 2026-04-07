# Deployment Guide

## Local Development

Use the quick start script:

```bash
./start.sh
```

Or manually:

```bash
source venv/bin/activate
python main.py
```

## Production Options

### Option 1: systemd (Linux)

Create `/etc/systemd/system/uptime-monitor.service`:

```ini
[Unit]
Description=Uptime Monitor API
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/uptime-api
Environment="PATH=/opt/uptime-api/venv/bin"
ExecStart=/opt/uptime-api/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable uptime-monitor
sudo systemctl start uptime-monitor
sudo systemctl status uptime-monitor
```

### Option 2: Docker

Create `Dockerfile`:

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 8000

CMD ["python", "main.py"]
```

Build and run:

```bash
docker build -t uptime-monitor .
docker run -d -p 8000:8000 -v $(pwd)/uptime.db:/app/uptime.db uptime-monitor
```

### Option 3: uvicorn with Gunicorn (Production)

Install additional dependencies:

```bash
pip install gunicorn
```

Run with multiple workers:

```bash
gunicorn main:app \
  --workers 1 \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --access-logfile - \
  --error-logfile -
```

**Note:** Keep workers=1 since we use APScheduler (only one instance should run the background scheduler).

### Option 4: Nginx Reverse Proxy

Nginx config (`/etc/nginx/sites-available/uptime-monitor`):

```nginx
server {
    listen 80;
    server_name monitor.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Enable and reload:

```bash
sudo ln -s /etc/nginx/sites-available/uptime-monitor /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### Option 5: Railway / Render / Fly.io

Most PaaS providers need a `Procfile`:

```
web: python main.py
```

Or use uvicorn directly:

```
web: uvicorn main:app --host 0.0.0.0 --port $PORT
```

Update `main.py` to read port from env:

```python
import os

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
```

## Environment Variables

Create `.env` file:

```bash
DATABASE=/data/uptime.db
PORT=8000
HOST=0.0.0.0
```

Update `main.py` to load them:

```python
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE = os.getenv("DATABASE", "uptime.db")
```

## Database Backups

### Automated SQLite Backup

Add to crontab:

```bash
# Backup database every 6 hours
0 */6 * * * cp /opt/uptime-api/uptime.db /opt/uptime-api/backups/uptime-$(date +\%Y\%m\%d-\%H\%M).db

# Keep only last 7 days
0 0 * * * find /opt/uptime-api/backups -name "uptime-*.db" -mtime +7 -delete
```

### Manual Backup

```bash
sqlite3 uptime.db ".backup uptime-backup.db"
```

## SSL/HTTPS

Use Let's Encrypt with certbot:

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d monitor.yourdomain.com
```

Auto-renewal is configured automatically.

## Monitoring the Monitor

Set up health checks:

```bash
# Add to crontab
*/5 * * * * curl -f http://localhost:8000/ || systemctl restart uptime-monitor
```

Or use external monitoring:
- UptimeRobot
- Pingdom
- Better Stack

## Performance Tuning

### Database Optimization

```sql
-- Add indexes for faster queries
CREATE INDEX IF NOT EXISTS idx_monitors_api_key ON monitors(api_key);
CREATE INDEX IF NOT EXISTS idx_monitors_plan ON monitors(plan);
CREATE INDEX IF NOT EXISTS idx_checks_success ON checks(success);
```

### Scheduler Tuning

Edit `main.py`:

```python
# Adjust check frequency (default: every 30 seconds)
scheduler.add_job(check_all_monitors, "interval", seconds=30)

# For higher load, increase interval to reduce overhead
scheduler.add_job(check_all_monitors, "interval", seconds=60)
```

## Scaling

For high-volume monitoring:

1. **Separate scheduler from API:**
   - Run API with multiple workers (read-only)
   - Run scheduler as single background process

2. **Use Redis for state:**
   - Share state between API instances
   - Distributed locking for checks

3. **PostgreSQL instead of SQLite:**
   - Better concurrency
   - Connection pooling

4. **Queue-based checking:**
   - Use Celery or RQ
   - Distribute checks across workers

## Security Checklist

- [ ] Use HTTPS in production
- [ ] Set up firewall (only 80/443 open)
- [ ] Run as non-root user
- [ ] Keep dependencies updated
- [ ] Regular database backups
- [ ] Rate limiting on API endpoints
- [ ] Monitor logs for suspicious activity
- [ ] Rotate API keys periodically

## Troubleshooting

### Server won't start

```bash
# Check if port is in use
lsof -i :8000

# Check logs
tail -f server.log
```

### Database locked

```bash
# Find process holding the lock
lsof uptime.db

# Make sure only one instance is running
ps aux | grep "python main.py"
```

### High CPU usage

- Reduce check frequency
- Increase scheduler interval
- Add indexes to database
- Monitor number of active monitors

## Logs

View logs in real-time:

```bash
tail -f server.log

# Or with uvicorn access logs
tail -f access.log
```

Rotate logs with logrotate:

```
/opt/uptime-api/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
}
```
