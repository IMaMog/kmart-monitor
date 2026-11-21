"""Kmart Pokemon TCG monitor.

Polls the product search API that kmart.com.au uses (Constructor.io) for Pokemon
trading cards sold by Kmart itself (no marketplace sellers), posts Discord alerts
for new listings and restocks, and keeps a "currently available" board up to date.

No browser or cookies needed: the search API is public and takes the same public
key the Kmart website sends. kmart.com.au itself sits behind a bot check, so this
deliberately never touches it.

Standard library only, so it runs on a bare GitHub Actions runner.

Usage:
    python monitor.py               # normal run (used by the workflow)
    python monitor.py --dry-run     # fetch + diff, print payloads, send nothing, save nothing
    python monitor.py --test-alert  # send one sample alert so you can see the format

Environment:
    DISCORD_WEBHOOK_URL        required  channel that receives alerts
    DISCORD_BOARD_WEBHOOK_URL  optional  channel for the live board (defaults to the alert webhook)
    DISCORD_PING               optional  mention added to alerts, e.g. <@123> or <@&456>
    INCLUDE_ACCESSORIES        optional  "true" to also track sleeves/binders/pages/toploaders
    KMART_PROXY                optional  fallback proxy if direct requests are blocked: host:port:user:pass
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------- config

# Constructor.io browse endpoint for Kmart's "Pokemon Trading Cards" category
# (kmart.com.au/category/toys/pokemon-trading-cards/).
API_URL = "https://ac.cnstrc.com/browse/group_id/abfdf5b2d48e682ca75bfe87a0ecba17"
# Public key the kmart.com.au frontend sends with every search request.
API_KEY = "key_GZTqlLr41FS2p7AY"
API_CLIENT = "ciojs-client-2.77.1"
API_HEADERS = {
    "accept": "application/json",
    "accept-encoding": "gzip",  # ~70 KB instead of ~270 KB; matters when going through a metered proxy
    "origin": "https://www.kmart.com.au",
    "referer": "https://www.kmart.com.au/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
    ),
}
SITE = "https://www.kmart.com.au"
BOARD_URL = SITE + "/category/toys/pokemon-trading-cards/"

STATES = ("ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA")

# Kmart's FulfilmentChannel. 3 = in-store only (sleeved boosters, cheap accessories):
# there's no online stock to watch, so those are tracked for new listings only.
CHANNEL_IN_STORE_ONLY = 3

# Title words that mark an accessory rather than sealed card product.
ACCESSORY_WORDS = ("portfolio", "pocket pages", "sleeves", "deck protector", "toploader",
                   "deck box", "playmat", "card storage")

STATUS_STYLE = {
    # status:      (label,           colour,   emoji)
    "preorder":    ("Pre-order",      0x8B5CF6, "🟣"),
    "in_stock":    ("In stock",       0x22C55E, "🟢"),
    "limited":     ("Limited stock",  0xF59E0B, "🟠"),
    "instore":     ("In-store only",  0x3B82F6, "🔵"),
    "unavailable": ("Sold out",       0x6B7280, "⚫"),
}
AVAILABLE = {"preorder", "in_stock", "limited", "instore"}

# --------------------------------------------------------------------------- HTTP


def http_json(url: str, body: dict | None, headers: dict, method: str = "POST", retries: int = 3):
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                if attempt < retries:
                    wait = float(e.headers.get("Retry-After") or 2 * attempt)
                    time.sleep(wait)
                    continue
            return e.code, _safe_json(e.read())
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
            time.sleep(2 * attempt)
    raise RuntimeError("unreachable")


def _safe_json(raw: bytes):
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")[:500]


# --------------------------------------------------------------------------- proxy


def proxy_url(raw: str) -> str | None:
    """Turn KMART_PROXY into a proxy URL.

    Accepts host:port:user:pass (the usual proxy-provider export), host:port,
    or a full URL such as http://user:pass@host:port.
    """
    raw = raw.strip()
    if not raw:
        return None
    if "://" in raw:
        return raw
    parts = raw.split(":")
    if len(parts) == 2:
        host, port = parts
        return f"http://{host}:{port}"
    if len(parts) >= 4:
        host, port, user = parts[0], parts[1], parts[2]
        password = ":".join(parts[3:])  # passwords may contain ':'
        q = lambda s: urllib.parse.quote(s, safe="")
        return f"http://{q(user)}:{q(password)}@{host}:{port}"
    raise ValueError("KMART_PROXY must be host:port:user:pass, host:port, or a full proxy URL")


_PROXY_OPENER: urllib.request.OpenerDirector | None = None
_PROXY_CHECKED = False
DIRECT_OPENER = urllib.request.build_opener()


def proxy_opener() -> urllib.request.OpenerDirector | None:
    """Opener through KMART_PROXY, or None if it isn't set. Discord traffic never uses the proxy."""
    global _PROXY_OPENER, _PROXY_CHECKED
    if not _PROXY_CHECKED:
        _PROXY_CHECKED = True
        raw = os.environ.get("KMART_PROXY", "")
        if raw.strip():
            # Shape only, never the value, so a bad paste can be diagnosed from the run log.
            s = raw.strip()
            print(f"KMART_PROXY shape: {len(s.split(':'))} ':'-separated parts, {len(s)} chars, "
                  f"scheme={'yes' if '://' in s else 'no'}, lines={len(s.splitlines())}, "
                  f"inner whitespace={'yes' if any(c.isspace() for c in s) else 'no'}")
        url = proxy_url(raw)
        if url:
            _PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({"http": url, "https": url}))
            print(f"Proxy {url.rsplit('@', 1)[-1]} configured as fallback for Kmart requests")
    return _PROXY_OPENER


class KmartBlocked(RuntimeError):
    """The search API refused the request (403/429) or the route failed, as opposed to a bug on our side."""


def api_get(url: str, via_proxy: bool) -> dict:
    """GET the search API (gzip), directly or through KMART_PROXY. Logs timing per attempt."""
    opener = proxy_opener() if via_proxy else DIRECT_OPENER
    via = "proxy" if via_proxy else "direct"
    retries = 3 if via_proxy else 2
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers=API_HEADERS)
        t0 = time.time()
        try:
            with opener.open(req, timeout=30) as resp:
                raw = resp.read()
                encoding = resp.headers.get("Content-Encoding", "")
            body = gzip.decompress(raw) if encoding == "gzip" else raw
            print(f"  GET {via} attempt {attempt}: HTTP {resp.status}, {time.time() - t0:.1f}s, "
                  f"{len(raw) // 1024} KB sent, {len(body) // 1024} KB unpacked")
            try:
                data = json.loads(body)
            except ValueError:
                data = None
            if not isinstance(data, dict) or "response" not in data:
                # A 200 that isn't the search JSON is a block/challenge page, like Target's Akamai page.
                raise KmartBlocked(f"Kmart search via {via}: not search JSON (block page?): "
                                   f"{body[:120].decode(errors='replace')!r}")
            return data
        except urllib.error.HTTPError as e:
            print(f"  GET {via} attempt {attempt}: HTTP {e.code} after {time.time() - t0:.1f}s")
            if e.code in (403, 429):
                raise KmartBlocked(f"Kmart search via {via}: HTTP {e.code}") from None
            if e.code >= 500 and attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise RuntimeError(f"Kmart search via {via} failed: HTTP {e.code}: {_safe_json(e.read())}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"  GET {via} attempt {attempt}: {type(e).__name__}: {getattr(e, 'reason', e)} "
                  f"after {time.time() - t0:.1f}s")
            if attempt == retries:
                raise KmartBlocked(f"Kmart search via {via}: {type(e).__name__}") from None
            time.sleep(2 * attempt)
    raise RuntimeError("unreachable")


# --------------------------------------------------------------------------- Kmart API

# Constructor's anonymous visitor id. The site makes one up per browser; any UUID works.
CLIENT_ID = str(uuid.uuid4())


def fetch_products(include_accessories: bool) -> dict[str, dict]:
    products: dict[str, dict] = {}
    page, per_page = 1, 200
    # Every check starts direct; KMART_FORCE_PROXY=1 (the workflow's proxy_only box) tests the proxy route.
    via_proxy = os.environ.get("KMART_FORCE_PROXY") == "1" and proxy_opener() is not None
    while page <= 10:  # page guard
        params = {
            "c": API_CLIENT,
            "key": API_KEY,
            "i": CLIENT_ID,
            "s": 1,
            "page": page,
            "num_results_per_page": per_page,
            "filters[Seller]": "Kmart",   # sold by Kmart, not marketplace sellers
            "sort_by": "relevance",
            "sort_order": "descending",
            "_dt": int(time.time() * 1000),  # cache buster, as the site sends
        }
        url = API_URL + "?" + urllib.parse.urlencode(params)
        try:
            data = api_get(url, via_proxy)
        except KmartBlocked as e:
            if via_proxy or not proxy_opener():
                raise
            print(f"  direct request failed ({e}); retrying through the proxy")
            via_proxy = True  # stay on the proxy for the rest of this check
            data = api_get(url, via_proxy)
        resp = data["response"]
        results = resp.get("results") or []
        for hit in results:
            d = hit.get("data") or {}
            if "Kmart" not in (d.get("Seller") or []):
                continue
            title = (hit.get("value") or "").strip()
            if "pokemon" not in title.lower() and d.get("Brand") != "Pokemon":
                continue
            if not include_accessories and is_accessory(title):
                continue
            p = normalise(hit)
            products[p["sku"]] = p
        if page * per_page >= int(resp.get("total_num_results") or 0) or not results:
            break
        page += 1
    return products


def is_accessory(title: str) -> bool:
    t = title.lower()
    return any(w in t for w in ACCESSORY_WORDS)


def current_price(d: dict) -> tuple[float, float | None]:
    """(price, was_price). A promo only counts while today is inside its date window."""
    price = float(d.get("price") or 0)
    today = date.today().isoformat()
    list_price = None
    promo = None
    for p in d.get("prices") or []:
        try:
            amount = float(p.get("amount"))
        except (TypeError, ValueError):
            continue
        if p.get("type") == "list":
            list_price = amount
        elif p.get("type") == "promo" and (p.get("startDate") or "") <= today <= (p.get("endDate") or ""):
            promo = amount if promo is None else min(promo, amount)
    # Kmart's own offer price, if the product also has marketplace offers.
    for o in d.get("offers") or []:
        if o.get("shopId") == 1 and o.get("prices"):
            price = float(o["prices"][0].get("originPrice") or price)
    if promo is not None and list_price and promo < list_price:
        return promo, list_price
    if list_price and list_price > price:
        return price, list_price
    return price, None


def normalise(hit: dict) -> dict:
    d = hit.get("data") or {}
    # stateOOS lists the states where Kmart is out of stock online, e.g. {"NT": "6", "WA": "6"}.
    oos = set((d.get("stateOOS") or {}).keys())
    states_in = [s for s in STATES if s not in oos]
    channel = d.get("FulfilmentChannel")

    release = d.get("preOrderReleaseDate")
    if release and release < "2001":
        release = None  # Kmart uses 2000-01-01 as "no date"

    if channel == CHANNEL_IN_STORE_ONLY:
        status = "instore"
        states_in = []
    elif not states_in:
        status = "unavailable"
    elif d.get("isPreOrderActive"):
        status = "preorder"
    elif len(states_in) == len(STATES):
        status = "in_stock"
    else:
        status = "limited"

    price, was = current_price(d)
    url = d.get("url") or d.get("uri") or ""
    return {
        "sku": str(d.get("variation_id") or d.get("id", "")).removeprefix("P_"),
        "title": (hit.get("value") or "").strip(),
        "url": SITE + url if url.startswith("/") else url,
        "image": d.get("image_url") or "",
        "price": price,
        "was_price": was,
        "status": status,
        "states": states_in,
        "release": release,
        "clearance": bool(d.get("clearance")),
    }


# --------------------------------------------------------------------------- formatting


def money(v: float | None) -> str:
    if v is None:
        return "—"
    return f"${v:,.0f}" if float(v).is_integer() else f"${v:,.2f}"


def short_title(title: str) -> str:
    for prefix in ("Pokemon Trading Card Game: ", "Pokemon Trading Card Game - ", "Pokemon TCG: ",
                   "Pokemon TCG - ", "Pokémon TCG: ", "Pokemon TCG "):
        if title.startswith(prefix):
            return title[len(prefix):]
    return title


def ts(iso: str | None, style: str = "D") -> str | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt.timestamp())}:{style}>"


def is_future(iso: str | None) -> bool:
    if not iso:
        return False
    return iso[:10] > date.today().isoformat()


def where_text(p: dict) -> str | None:
    if p["status"] == "instore":
        return "In-store only"
    return states_text(p["states"])


def states_text(states: list[str]) -> str | None:
    """Where it's in stock online, in words: 'All states', 'Only in NSW, WA', 'All states except NT'."""
    if not states:
        return None
    if len(states) == len(STATES):
        return "All states"
    missing = [s for s in STATES if s not in states]
    if len(missing) < len(states):
        return "All states except " + ", ".join(missing)
    return "Only in " + ", ".join(states)


def alert_embed(p: dict, kind: str) -> dict:
    label, colour, emoji = STATUS_STYLE[p["status"]]
    heading = {"new": "New listing", "restock": "Back in stock"}[kind]

    price = money(p["price"])
    if p["was_price"]:
        price += f"  ~~{money(p['was_price'])}~~"

    fields = [
        {"name": "Price", "value": price, "inline": True},
        {"name": "Status", "value": f"{emoji} {label}", "inline": True},
    ]
    if p["status"] == "preorder" or is_future(p["release"]):
        rel = ts(p["release"], "D")
        if rel:
            fields.append({"name": "Release", "value": f"{rel} ({ts(p['release'], 'R')})", "inline": True})
    where = where_text(p)
    if where:
        fields.append({"name": "Available", "value": where, "inline": False})
    if p["clearance"]:
        fields.append({"name": "Note", "value": "Clearance", "inline": True})

    embed = {
        "author": {"name": f"Kmart  •  {heading}"},
        "title": p["title"][:256],
        "url": p["url"],
        "color": colour,
        "fields": fields,
        "footer": {"text": f"Keycode {p['sku']}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if p["image"]:
        embed["thumbnail"] = {"url": p["image"]}
    return embed


BOARD_SECTIONS = [
    ("preorder", "Pre-orders"),
    ("in_stock", "In stock"),
    ("limited", "Limited stock"),
    ("instore", "In-store only"),
]


def board_embeds(products: dict[str, dict]) -> list[dict]:
    now = int(time.time())
    live = [p for p in products.values() if p["status"] in AVAILABLE]

    header = {
        "title": "Kmart — Pokémon TCG available now",
        "url": BOARD_URL,
        "color": 0xE4002B,
        "description": (
            f"**{len(live)}** products live"
            + "".join(
                f"  ·  {STATUS_STYLE[s][2]} {sum(1 for p in live if p['status'] == s)}"
                for s, _ in BOARD_SECTIONS
            )
            + f"\nLast checked <t:{now}:R>"
        ),
    }
    embeds = [header]
    for status, _ in BOARD_SECTIONS:
        items = sorted(
            (p for p in live if p["status"] == status),
            key=lambda p: (p["release"] or "", p["title"]) if status == "preorder" else (-p["price"], p["title"]),
        )
        embeds.extend(board_card(p) for p in items)

    if len(embeds) == 1:
        header["description"] += "\n\nNothing available right now."
    return embeds


def board_card(p: dict) -> dict:
    """One compact card per product: image on the right, colour = status."""
    label, colour, emoji = STATUS_STYLE[p["status"]]
    line1 = [f"**{money(p['price'])}**", f"{emoji} {label}"]
    if p["status"] == "preorder" or is_future(p["release"]):
        rel = ts(p["release"], "D")
        if rel:
            line1.append(f"out {rel}")
    line2 = []
    if p["status"] in ("limited", "preorder") and len(p["states"]) < len(STATES):
        line2.append(states_text(p["states"]))
    if p["clearance"]:
        line2.append("Clearance")
    card = {
        "title": short_title(p["title"])[:256],
        "url": p["url"],
        "color": colour,
        "description": " · ".join(line1) + ("\n" + " · ".join(line2) if line2 else ""),
    }
    if p["image"]:
        card["thumbnail"] = {"url": p["image"]}
    return card


# --------------------------------------------------------------------------- Discord

DISCORD_UA = "kmart-tcg-monitor"
BOT_NAME = "Kmart Monitor"


def webhook_parts(url: str) -> str:
    return url.split("?")[0].rstrip("/")


def discord_send(webhook: str, payload: dict) -> str | None:
    url = webhook_parts(webhook) + "?wait=true"
    status, data = http_json(url, payload, {"content-type": "application/json", "user-agent": DISCORD_UA})
    if status not in (200, 204):
        raise RuntimeError(f"Discord send failed: HTTP {status}: {data}")
    return (data or {}).get("id")


def discord_edit(webhook: str, message_id: str, payload: dict) -> bool:
    url = f"{webhook_parts(webhook)}/messages/{message_id}"
    status, data = http_json(url, payload, {"content-type": "application/json", "user-agent": DISCORD_UA},
                             method="PATCH")
    if status == 404:
        return False
    if status != 200:
        raise RuntimeError(f"Discord edit failed: HTTP {status}: {data}")
    return True


def send_alerts(webhook: str, ping: str, alerts: list[tuple[dict, str]]) -> None:
    # Discord allows 10 embeds per message; batch so a big drop is one or two messages.
    for i in range(0, len(alerts), 10):
        batch = alerts[i:i + 10]
        payload = {
            "username": BOT_NAME,
            "embeds": [alert_embed(p, kind) for p, kind in batch],
            "allowed_mentions": {"parse": ["users", "roles"]},
        }
        if ping:
            payload["content"] = ping
        discord_send(webhook, payload)
        time.sleep(1)


def discord_delete(webhook: str, message_id: str) -> None:
    url = f"{webhook_parts(webhook)}/messages/{message_id}"
    status, data = http_json(url, None, {"user-agent": DISCORD_UA}, method="DELETE")
    if status not in (204, 404):
        raise RuntimeError(f"Discord delete failed: HTTP {status}: {data}")


def update_board(webhook: str, state: dict, products: dict[str, dict]) -> None:
    embeds = board_embeds(products)
    # Discord caps a message at 10 embeds: header + 9 cards in the first, 10 cards in each after.
    chunks = [embeds[i:i + 10] for i in range(0, len(embeds), 10)]
    payloads = [{"embeds": c, "allowed_mentions": {"parse": []}} for c in chunks]
    ids = state.get("board_message_ids") or []

    if len(ids) == len(payloads):
        ok = True
        for msg_id, payload in zip(ids, payloads):
            if not discord_edit(webhook, msg_id, payload):
                ok = False  # someone deleted a board message
                break
            time.sleep(0.5)
        if ok:
            state["board_message_ids"] = ids
            return

    # Message count changed (or a message vanished): repost the whole board so it stays contiguous.
    for msg_id in ids:
        discord_delete(webhook, msg_id)
        time.sleep(0.5)
    new_ids = []
    for payload in payloads:
        new_ids.append(discord_send(webhook, {**payload, "username": BOT_NAME}))
        time.sleep(0.5)
    state["board_message_ids"] = new_ids
    print(f"Posted board as {len(new_ids)} message(s) {new_ids} — pin the first one in Discord.")


# --------------------------------------------------------------------------- state + diff


def load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(state: dict, path: Path) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def diff(prev: dict[str, dict], current: dict[str, dict]) -> list[tuple[dict, str]]:
    alerts = []
    for sku, p in current.items():
        old = prev.get(sku)
        if old is None:
            if p["status"] in AVAILABLE:
                alerts.append((p, "new"))
        elif p["status"] in AVAILABLE and old.get("status") not in AVAILABLE:
            alerts.append((p, "restock"))
    return alerts


def merge(prev: dict[str, dict], current: dict[str, dict]) -> dict[str, dict]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    merged = {}
    for sku, p in current.items():
        # No per-run timestamps here, so state.json only changes (and gets committed) on real changes.
        merged[sku] = {**p, "first_seen": prev.get(sku, {}).get("first_seen", now)}
    for sku, old in prev.items():
        if sku not in current:
            # Dropped out of the category (delisted, or moved to marketplace-only).
            merged[sku] = {**old, "status": "unavailable", "states": []}
    return merged


# --------------------------------------------------------------------------- timing

INTERVAL_MIN = 15      # check on every quarter hour...
OFFSET_SEC = 1         # ...one second past it: 4:00:01, 4:15:01, ...


def slot_for(now: float, cron_minute: int | None) -> float:
    """The check time this run is responsible for.

    With the cron minute that triggered the run (e.g. 53), the slot is the first quarter hour
    after the most recent firing of that minute, so a run GitHub started late still knows which
    slot it was meant for. Without it, fall back to the next quarter hour.
    """
    period = INTERVAL_MIN * 60
    if cron_minute is None:
        return (now - OFFSET_SEC) // period * period + period + OFFSET_SEC
    hour = now // 3600 * 3600
    fired = hour + cron_minute * 60
    if fired > now:
        fired -= 3600
    return (fired // period) * period + period + OFFSET_SEC


def wait_for_slot(cron_minute: int | None) -> None:
    now = time.time()
    slot = slot_for(now, cron_minute)
    wait = slot - now
    when = datetime.fromtimestamp(slot, timezone.utc).strftime("%H:%M:%S UTC")
    if wait <= 0:
        # GitHub started us after the slot; check now rather than skip it.
        print(f"Started {-wait:.0f}s after the {when} slot; checking immediately.")
        return
    print(f"Waiting {wait:.1f}s for {when}")
    while (remaining := slot - time.time()) > 0:
        time.sleep(min(remaining, 30))


# --------------------------------------------------------------------------- main


LOG_DIR = Path(__file__).with_name("logs")


def load_dotenv(path: Path) -> None:
    """Minimal .env loader for local runs (KEY=VALUE lines). Real env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def log_csv(name: str, header: list[str], row: list) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / name
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def process(state: dict, current: dict[str, dict], cfg: dict, dry_run: bool,
            log_alerts: bool = False) -> tuple[dict, list[tuple[dict, str]]]:
    """Diff a fetch against state, send alerts, return (merged products, alerts)."""
    prev = state.get("products", {})
    first_run = not prev and not state.get("seeded")
    alerts = [] if first_run else diff(prev, current)
    merged = merge(prev, current)
    if first_run:
        print("First run: seeding state without alerts.")
    detected = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for p, kind in alerts:
        print(f"[{kind}] {p['title']} — {money(p['price'])} — {p['status']}")
        if log_alerts:
            log_csv("alerts.csv", ["detected_utc", "kind", "sku", "title", "status", "states"],
                    [detected, kind, p["sku"], p["title"], p["status"], " ".join(p["states"])])
    if alerts and not dry_run:
        send_alerts(cfg["alert_hook"], cfg["ping"], alerts)
    state["seeded"] = True
    return merged, alerts


POLL_HEADER = ["utc", "ok", "ms", "products", "available", "error"]


def fetch_logged(cfg: dict) -> dict[str, dict]:
    t0 = time.time()
    started = datetime.now(timezone.utc)
    try:
        current = fetch_products(cfg["include_acc"])
    except Exception as e:
        if cfg.get("log_polls"):
            log_csv("polls.csv", POLL_HEADER,
                    [started.isoformat(timespec="seconds"), 0, int((time.time() - t0) * 1000), "", "", str(e)[:200]])
        raise
    ms = int((time.time() - t0) * 1000)
    avail = sum(p["status"] in AVAILABLE for p in current.values())
    if cfg.get("log_polls"):
        log_csv("polls.csv", POLL_HEADER, [started.isoformat(timespec="seconds"), 1, ms, len(current), avail, ""])
    print(f"[{started.strftime('%H:%M:%S.%f')[:-3]} UTC] Fetched {len(current)} products "
          f"({avail} available) in {ms} ms")
    return current


def run_once(cfg: dict, state_path: Path, dry_run: bool) -> int:
    current = fetch_logged(cfg)
    state = load_state(state_path) or {"products": {}}
    if not current and state.get("products"):
        print("API returned no products; skipping this run to avoid false sold-out states.")
        return 0
    merged, _ = process(state, current, cfg, dry_run)
    if dry_run:
        print(json.dumps({"embeds": board_embeds(merged)}, indent=2, ensure_ascii=False))
        return 0
    state["products"] = merged
    update_board(cfg["board_hook"], state, merged)
    save_state(state, state_path)
    return 0


def run_loop(cfg: dict, state_path: Path, interval: float, dry_run: bool) -> int:
    """Poll forever. Board is refreshed when something changes, or at most once a minute."""
    board_every = 60
    state = load_state(state_path) or {"products": {}}
    last_board = 0.0
    failures = 0
    print(f"Loop mode: polling every ~{interval:g}s (±20%), state in {state_path.name}. Ctrl-C to stop.")
    while True:
        t0 = time.time()
        try:
            current = fetch_logged(cfg)
            failures = 0
            if not current and state.get("products"):
                print("  empty result; ignoring")
            else:
                before = json.dumps(state.get("products", {}), sort_keys=True)
                ids_before = state.get("board_message_ids")
                merged, _ = process(state, current, cfg, dry_run, log_alerts=True)
                state["products"] = merged
                changed = json.dumps(merged, sort_keys=True) != before
                if not dry_run and (changed or time.time() - last_board >= board_every):
                    update_board(cfg["board_hook"], state, merged)
                    last_board = time.time()
                if not dry_run and (changed or state.get("board_message_ids") != ids_before):
                    save_state(state, state_path)
            delay = interval * random.uniform(0.8, 1.2) - (time.time() - t0)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            failures += 1
            delay = min(300, interval * 2 ** failures)  # back off hard on errors / 429s
            print(f"  error #{failures}: {e} — backing off {delay:.0f}s", file=sys.stderr)
        time.sleep(max(1.0, delay))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        # UTF-8 for emoji on Windows consoles; line buffering so loop output shows up live in logs.
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="don't send to Discord or write state")
    ap.add_argument("--test-alert", action="store_true", help="send one sample alert and exit")
    ap.add_argument("--at-quarter", action="store_true",
                    help="sleep until the next :00:01/:15:01/:30:01/:45:01 before checking")
    ap.add_argument("--schedule", default="",
                    help='cron expression that triggered the run (GitHub passes e.g. "53 * * * *")')
    ap.add_argument("--loop", type=float, metavar="SECONDS",
                    help="poll continuously every SECONDS (±20%%), logging to logs/")
    ap.add_argument("--state", default="state.json", help="state file (use a separate one for local trials)")
    ap.add_argument("--env-file", default=".env", help="local file with DISCORD_WEBHOOK_URL=... etc.")
    args = ap.parse_args()

    here = Path(__file__).parent
    load_dotenv(here / args.env_file)
    alert_hook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    cfg = {
        "alert_hook": alert_hook,
        "board_hook": os.environ.get("DISCORD_BOARD_WEBHOOK_URL", "").strip() or alert_hook,
        "ping": os.environ.get("DISCORD_PING", "").strip(),
        "include_acc": os.environ.get("INCLUDE_ACCESSORIES", "").lower() in ("1", "true", "yes"),
        "log_polls": bool(args.loop),
    }
    state_path = Path(args.state) if Path(args.state).is_absolute() else here / args.state

    if not alert_hook and not args.dry_run:
        print("DISCORD_WEBHOOK_URL is not set (env var or .env file)", file=sys.stderr)
        return 2

    if args.test_alert:
        current = fetch_logged(cfg)
        sample = next((p for p in current.values() if p["status"] in AVAILABLE), None)
        if not sample:
            print("Nothing available to use as a sample.")
            return 1
        send_alerts(alert_hook, cfg["ping"], [(sample, "new")])
        print(f"Sent test alert for {sample['title']}")
        return 0

    if args.loop:
        if args.loop < 5:
            print("--loop below 5 seconds is asking to get rate-limited; use 5 or more.", file=sys.stderr)
            return 2
        try:
            return run_loop(cfg, state_path, args.loop, args.dry_run)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0

    if args.at_quarter:
        first = args.schedule.split()[0] if args.schedule.strip() else ""
        wait_for_slot(int(first) if first.isdigit() else None)
    return run_once(cfg, state_path, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
