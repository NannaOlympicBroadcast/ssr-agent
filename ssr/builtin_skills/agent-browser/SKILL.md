---
name: agent-browser
description: Drive a real browser for web automation and research using Vercel's agent-browser (https://github.com/vercel-labs/agent-browser) — navigate, click, type, extract and screenshot.
---

# Agent Browser Skill

Give the agent a controllable headless/headed browser for tasks that need a real
page: logging in, scraping JS-rendered content, filling forms, taking screenshots.

## Install
```bash
npm install -g @vercel-labs/agent-browser
# or run ad-hoc
npx @vercel-labs/agent-browser
```

## Usage pattern
1. Start the browser agent server (it exposes a local control endpoint).
2. From SSR, call it via `run_command`, e.g.:
   ```bash
   agent-browser open "https://example.com"
   agent-browser act "click the login button"
   agent-browser extract "the article title and author"
   agent-browser screenshot ./shot.png
   ```

## When to use
Prefer `web_search` (Tavily) for quick factual lookups. Reach for **agent-browser**
when you need to *interact* with a site — authenticated flows, dynamic content,
multi-step navigation, or visual verification via screenshots.

## Notes
- Honour the target site's terms of service and robots policy.
- Keep credentials out of the repo; read them from the environment.
