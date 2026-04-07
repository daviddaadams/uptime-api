# OwlPulse Image Assets

## OG Images (Social Share)

### Dev.to / Social Cards
- **Size**: 1200 x 630 px
- **Format**: PNG or JPG
- **Template**: `devto-og-template.html` (replace {{title}} and {{subtitle}})

### Twitter/X Card
- **Size**: 1200 x 675 px (2:1 ratio)
- **Format**: PNG or JPG

### LinkedIn / Facebook
- **Size**: 1200 x 630 px
- **Format**: PNG or JPG

## Brand Colors
| Name | Hex | Usage |
|------|-----|-------|
| Background | #0f172a | Main bg |
| Surface | #1e293b | Cards, containers |
| Accent | #f97316 | CTAs, highlights |
| Text | #f8fafc | Primary text |
| Muted | #94a3b8 | Secondary text |

## Generating Images

### Option 1: Screenshot the template
1. Open `devto-og-template.html` in a browser
2. Replace {{title}} and {{subtitle}} with your content
3. Screenshot at 2x resolution (2400x1260) for crisp rendering

### Option 2: Use a CLI tool
```bash
# Install chromium or use puppeteer
npx puppeteer screenshot
```

## Logo Usage
- Full: "🦉 OwlPulse" 
- Icon only: "🦉"
- Minimum clear space: 12px all sides
