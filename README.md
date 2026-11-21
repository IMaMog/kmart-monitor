# Kmart Pokémon TCG monitor

Works the same way as the JB Hi-Fi monitor. GitHub Actions checks Kmart every 15 minutes, one second past each quarter hour (hh:00:01, hh:15:01, hh:30:01, hh:45:01). It looks for Pokémon TCG products **sold by Kmart itself** (marketplace sellers like Gameology are excluded) and posts to Discord:

- **Alerts**: an embed when a product is **newly listed** or comes **back in stock**. It shows price, status, release date for pre-orders, which states have online stock, and clearance.
- **Live board**: one card per available product, in the order Pre-orders → In stock → Limited stock → In-store only. The board is edited in place every run, so you pin it once. As with JB, it gets reposted (and needs re-pinning) when the product count crosses a multiple of 9 or 10. A dedicated board channel avoids pinning altogether.

## No browser or headers needed

The monitor calls `ac.cnstrc.com`, the Constructor.io search API that kmart.com.au uses to fill its category pages. That API is public:

| Param / header | What it is | What the monitor sends |
|---|---|---|
| `key=key_GZTqlLr41FS2p7AY` | Kmart's public search key, baked into the website | Same constant |
| `i=<uuid>` | Anonymous visitor ID the site makes up per browser | A fresh random UUID each run |
| `s=1` | Session counter | `1` |
| `_dt=<ms>` | Timestamp cache buster | Current time |
| `c=ciojs-client-2.77.1` | Client library version | Same constant |
| `origin` / `referer` / `user-agent` | Normal browser headers | Static copies (not required, sent to look ordinary) |

There are no cookies, tokens or signatures, so nothing expires and there's nothing to refresh from a live browser. kmart.com.au itself is behind a bot check, and the monitor never touches it. If Kmart ever rotates the key, the run fails, and the fix is to copy the new `key=` value from DevTools into `API_KEY` in `monitor.py`.

The `filters[Seller]=Kmart` parameter means a single request returns everything Kmart sells in the Pokémon cards category, which is about 85 items. Responses are requested gzipped, about 70 KB instead of 270 KB.

### Proxy fallback (optional)

Constructor doesn't block GitHub today, so every check goes direct first, which takes about half a second. If you set the `KMART_PROXY` secret, a check retries through the proxy when it's blocked or refused: HTTP 403/429, a block page in place of the search results, or a failed connection. It then stays on the proxy for the rest of that check, and the next check tries direct again. Discord requests never use the proxy. You can reuse the same proxy as the Target monitor.

- Format: `host:port:user:pass` (the usual provider export), `host:port`, or `http://user:pass@host:port`.
- The run log shows the proxy's host and the shape of the value, never the password, so a bad paste can be spotted.
- Manual runs have two test boxes: **no_proxy** (direct only) and **proxy_only** (straight through the proxy, to check it works).
- Data used through the proxy: about 70 KB per check, or about 200 MB a month if every scheduled check went through it.

## How stock is worked out

Constructor doesn't carry a plain "in stock" flag, so status comes from these fields:

- `stateOOS` lists the states where Kmart is out of stock online, for example `{"NT": "6", "WA": "6"}`.
  - No states listed → **In stock**.
  - Some states listed → **Limited stock**, and the board shows which states still have it.
  - All 8 listed → **Sold out**.
- `isPreOrderActive` → **Pre-order** (as long as at least one state has stock).
- `FulfilmentChannel = 3` → **In-store only**. This covers sleeved boosters and cheap accessories. There's no online stock to watch for these, so they alert when first listed but can't alert on restocks.

Set `KMART_STATE=NSW` (or whichever state you're in) to judge stock for your state only. A product then counts as in stock only if your state has it, so restock alerts mean "you can order it now".

> These field meanings were inferred from the data: brand-new products show most states out, and old sold-out sets show all 8 out. They haven't been compared against the live site yet. Spot-check one or two products against the board before trusting it.

By default only sealed product is tracked. Set `INCLUDE_ACCESSORIES=true` to also track sleeves, portfolios, pocket pages and toploaders.

## Setup

1. **Create the Discord webhooks** (Channel settings → Integrations → Webhooks). Use one for alerts (for example `#kmart-alerts`) and, optionally, a second for the board (`#kmart-stock`).

2. **Create a public GitHub repo and push this folder.** It has to be public for the same reason as JB: each run waits up to about 7 minutes on the runner, which uses about 20,000 runner-minutes a month. That's free on public repos only.

   ```bash
   git add .
   git commit -m "Kmart Pokemon monitor"
   git remote add origin https://github.com/<you>/kmart-monitor.git
   git push -u origin main
   ```

3. **Add the settings** under repo → Settings → Secrets and variables → Actions.

   | Kind | Name | Value |
   |---|---|---|
   | Secret | `DISCORD_WEBHOOK_URL` | Alerts webhook URL (required) |
   | Secret | `DISCORD_BOARD_WEBHOOK_URL` | Board webhook URL (optional) |
   | Variable | `DISCORD_PING` | `<@YOUR_USER_ID>` or `<@&ROLE_ID>` (optional) |
   | Variable | `INCLUDE_ACCESSORIES` | `true` to include accessories (optional) |
   | Variable | `KMART_STATE` | `NSW`, `VIC`, `QLD`, `WA`, `SA`, `TAS`, `ACT` or `NT` (optional) |
   | Secret | `KMART_PROXY` | Fallback proxy, `host:port:user:pass` (optional) |

4. **Start it:** Actions tab → *Kmart Pokemon monitor* → Run workflow. The first run records what's listed without alerting, then posts the board. Pin the board message. To preview an alert, run it again with **Send a sample alert** ticked.

## Running locally

Copy `.env.example` to `.env` and fill in a webhook. Use a test channel while trialling.

```bash
python monitor.py --dry-run                          # fetch + print, no Discord, no state written
python monitor.py --test-alert                       # send one sample alert
python monitor.py --loop 15 --state state.local.json # near-live polling
```

Loop mode works like JB's: it polls every ~15 s (±20%), backs off on errors, and logs to `logs/polls.csv` and `logs/alerts.csv`. Constructor allows about 200 requests per few-second window, so a 15 s loop sits far below the limit.

## Caveats

- Constructor's copy of Kmart's stock is a feed. It may trail the live site by a few minutes when stock changes.
- The scheduling caveats are the same as JB's. GitHub can start runs late or drop them, and it disables schedules after 60 days without repo activity.
