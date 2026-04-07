# OwlPulse Status Renderer

This directory contains status-page rendering artifacts used by the FastAPI backend.

## Files

- `template.html`: Base HTML template consumed by the `/status/{slug}` endpoint.
- `sample-rendered.html`: Example rendered output using sample values.

## Runtime Variables

The renderer injects values into `template.html` placeholders:

- `{{TITLE}}`, `{{DESCRIPTION}}`
- `{{OVERALL_STATUS_LABEL}}`, `{{UPTIME_90D}}`
- `{{MONITOR_ROWS}}`, `{{INCIDENT_ROWS}}`
- `{{ACCENT_COLOR}}`, `{{BACKGROUND_COLOR}}`, `{{TEXT_COLOR}}`
- `{{SUBSCRIBE_ENDPOINT}}`

## Integration Notes

1. Keep placeholders intact in `template.html` so the API can substitute values.
2. Supported themes: default, minimal, cyber, corporate, terminal.
3. Update design assets in `/Users/davidadams/owlpulse-design/templates/` first.
4. `GET /status/{slug}` renders public HTML.
5. `POST /status/{slug}/subscribe` stores subscriber emails per page.
