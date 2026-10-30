#!/usr/bin/env python3
"""Find new venue social posts and add the approved ones to index.html.

Sources:
  1. Social post links venues submit in their campaign recaps (BigQuery, mongodb.recaps).
  2. Posts starred in the Recap Media Browser photo tool (/social_posts).

Flow:
  python3 tools/refresh.py                 # last 180 days, all brands
  python3 tools/refresh.py --brand Midol   # one or more brands (page names or recap names)
  python3 tools/refresh.py --since 2026-06-01

The script drops posts already on the page or skipped in an earlier review, then
opens a local review page that renders every candidate the way the site will.
Tick the posts to add (dead posts show as "unavailable"), adjust event type /
category if needed, and hit Save. Approved posts are written into index.html and
_catalog.json; unticked ones go into tools/skipped.json so they don't come back.
Then preview the page and commit/push.

Other flags:
  --list                 print candidates and exit (no review page)
  --apply decisions.json apply a saved decisions file instead of opening the review page
  --include-skipped      show posts skipped in earlier reviews again
  --no-browser           print the review page URL instead of opening it
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "index.html")
CATALOG = os.path.join(ROOT, "_catalog.json")
SKIPPED = os.path.join(ROOT, "tools", "skipped.json")
REVIEW_TEMPLATE = os.path.join(ROOT, "tools", "review.html")
PHOTO_TOOL = "https://recap-photo-filter-390702212979.us-central1.run.app"
BQ_KEY = os.environ.get(
    "RECESS_BQ_KEY",
    os.path.expanduser("~/.claude/recess-tools/credentials/bigquery-service-account.json"),
)

CATEGORIES = ["Food", "Beverages", "Beauty", "Wellness", "Household"]
EVENT_TYPES = ["Fitness Studios", "Student Housing", "Coworking", "Festivals", "Social Sports", "Other"]

# Recap brand names -> the name the page uses.
BRAND_ALIASES = {
    "kraft macaroni & cheese": "Kraft Mac & Cheese",
    "mio": "Mio",
    "e.l.f. cosmetics": "e.l.f.",
    "e.l.f. haircare": "e.l.f.",
    "ghost energy": "Ghost",
    "starburst": "Starburst",
    "stokcoldbrew": "Stōk Cold Brew",
    "hero cosmetics": "Hero",
    "la colombe coffee roasters": "La Colombe",
    "gooderfoods": "Goodles",
    "dove men+ care deodorant": "Dove",
    "cleantopia mascara": "Cover Girl",
}

# Parent advertiser for brands whose recap sponsor is an agency/retailer, not the owner.
BRAND_ADVERTISERS = {
    "Starburst": "Mars",
    "Dove": "Unilever",
    "Peace Tea": "Coca-Cola",
    "Ghost": "Keurig Dr Pepper",
    "Hawaiian Tropic": "Edgewell",
    "Gatorade": "PepsiCo",
    "Chomps": "Chomps",
    "Vita Coco": "Vita Coco",
}

# Recap sponsor org name -> advertiser label used in the page's Advertiser filter.
SPONSOR_ADVERTISERS = {
    "bayer healthcare llc": "Bayer",
    "the kraft heinz company": "Kraft Heinz",
    "church & dwight co": "Church & Dwight",
    "danone north america": "Danone",
    "glanbia performance nutrition": "Glanbia",
    "e.l.f. cosmetics., inc.": "e.l.f. Beauty",
    "heineken usa": "Heineken",
    "calbee america": "Calbee",
    "coty inc.": "Coty",
    "kimberly-clark corporation": "Kimberly-Clark",
    "molson coors beverage company us llc": "Molson Coors",
    "chi forest (north america) inc": "Chi Forest",
    "bonduelle americas": "Bonduelle",
}

VENUE_EVENT_TYPES = {
    "university_housing": "Student Housing",
    "group_fitness_studio": "Fitness Studios",
    "boutique_wellness": "Fitness Studios",
    "conventional_gym": "Fitness Studios",
    "fitness_studio": "Fitness Studios",
    "coworking": "Coworking",
    "music_festival": "Festivals",
    "festival": "Festivals",
    "film_festival": "Festivals",
    "social_sports": "Social Sports",
    "active_recreation": "Social Sports",
}
FITNESS_WORDS = re.compile(r"gym|fitness|crunch|f45|yoga|pilates|cycle|spenga|barre|orangetheory|sweat|health ?club", re.I)

CATEGORY_TAGS = [
    ("Beverages", re.compile(r"beverage|coffee|tea|water|energy|juice|drink|beer|wine|spirit", re.I)),
    ("Beauty", re.compile(r"cosmetic|skin|beauty|hair|grooming|personal_care", re.I)),
    ("Wellness", re.compile(r"vitamin|supplement|health|pharma|wellness", re.I)),
    ("Household", re.compile(r"clean|household|home|laundry", re.I)),
    ("Food", re.compile(r"food|snack|candy|pasta|meat", re.I)),
]

FB_KEEP_PARAMS = {"fbid", "set", "story_fbid", "id", "v"}


# ---------- URL helpers ----------

def post_key(url):
    """Stable ID for a post so the same post matches across URL variants."""
    m = re.search(r"instagram\.com/(?:[^/]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", url)
    if m:
        return "ig:" + m.group(1)
    m = re.search(r"(pfbid\w+)", url)
    if m:
        return "fb:" + m.group(1)
    m = re.search(r"[?&](?:fbid|story_fbid|v)=(\d+)", url) or re.search(r"/(?:videos|reel|posts|photos)/(?:[^/]+/)?(\d{6,})", url)
    if m:
        return "fb:" + m.group(1)
    return None


def clean_url(url):
    url = url.rstrip(").;'")
    if "instagram.com" in url:
        m = re.search(r"instagram\.com/(?:[^/]+/)?(p|reel|reels|tv)/([A-Za-z0-9_-]+)", url)
        kind = "reel" if m.group(1) in ("reel", "reels") else "p"
        return f"https://www.instagram.com/{kind}/{m.group(2)}/"
    parts = urllib.parse.urlsplit(url.replace("m.facebook.com", "www.facebook.com"))
    q = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k in FB_KEEP_PARAMS]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(q), ""))


def embed_src(url):
    if "instagram.com" in url:
        return url + "embed/captioned/"
    return ("https://www.facebook.com/plugins/post.php?href="
            + urllib.parse.quote(url, safe="") + "&show_text=true&width=350")


def platform_of(url):
    return "instagram" if "instagram.com" in url else "facebook"


# ---------- page parsing ----------

def read_page():
    with open(INDEX, encoding="utf-8") as f:
        return f.read()


def page_post_keys(page):
    keys = set()
    for src in re.findall(r'<iframe src="([^"]+)"', page):
        src = html.unescape(src)
        m = re.search(r"href=([^&]+)", src)
        url = urllib.parse.unquote(m.group(1)) if m else src
        k = post_key(url)
        if k:
            keys.add(k)
    return keys


def page_brands(page):
    """Brands with their own brand-block -> category."""
    out = {}
    for tag in re.findall(r'<div class="brand-block[^"]*"[^>]*>', page):
        b = re.search(r'data-brand="([^"]+)"', tag)
        c = re.search(r'data-category="([^"]+)"', tag)
        if b:
            out[html.unescape(b.group(1))] = c.group(1) if c else None
    return out


def page_all_brands(page):
    """Every brand with a card anywhere (incl. the showcase) -> its category."""
    cards = re.findall(r'<article class="post-card[^"]*"\s+data-brand="([^"]+)"\s+data-category="([^"]+)"', page)
    return {html.unescape(b): c for b, c in cards}


def page_advertisers(page):
    m = re.search(r"var ADVERTISERS = \{(.*?)\};", page, re.S)
    return dict(re.findall(r'"([^"]+)":\s*"([^"]+)"', m.group(1))) if m else {}


# ---------- sources ----------

URL_RE = r'https?://(?:www\.|m\.)?(?:instagram\.com|facebook\.com|fb\.watch)/[^"\\\s,]+'


def fetch_recaps(since, recap_ids):
    from google.cloud import bigquery

    client = bigquery.Client.from_service_account_json(BQ_KEY)
    q = rf"""
    select _id, start_date,
      json_value(to_json_string(ctx),'$.campaign.brand_info.name') brand,
      json_value(to_json_string(ctx),'$.sponsor.org.name') sponsor,
      json_value(to_json_string(ctx),'$.supplier.org.name') supplier,
      json_value(to_json_string(ctx),'$.listing.venue_name') venue,
      json_value(to_json_string(ctx),'$.listing.primary_type') venue_type,
      to_json_string(json_query(to_json_string(ctx),'$.campaign.brand_info.category_tags')) cats,
      regexp_extract_all(concat(ifnull(to_json_string(answers),''), ifnull(to_json_string(published_answers),'')),
                         r'{URL_RE}') urls
    from `stitchdata-384118.mongodb.recaps`
    where date(start_date) >= @since or _id in unnest(@ids)
    """
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("since", "DATE", since),
        bigquery.ArrayQueryParameter("ids", "STRING", sorted(recap_ids)),
    ])
    return [dict(r) for r in client.query(q, job_config=cfg).result()]


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def fetch_photo_tool_posts():
    posts, page = [], 1
    while True:
        d = fetch_json(f"{PHOTO_TOOL}/social_posts?page={page}")
        posts += d.get("posts", [])
        pg = d.get("pagination", {})
        if page >= pg.get("total_pages", 1):
            return posts
        page += 1


_rights_cache = {}


def brand_rights(recap_brand):
    if recap_brand not in _rights_cache:
        try:
            d = fetch_json(f"{PHOTO_TOOL}/photos/search?per_page=1&brand={urllib.parse.quote(recap_brand)}")
            photos = d.get("photos") or [{}]
            _rights_cache[recap_brand] = (photos[0].get("brand_logo_usage") or "").strip()
        except Exception:
            _rights_cache[recap_brand] = ""
    return _rights_cache[recap_brand]


# ---------- candidate building ----------

def canonical_brand(name, known):
    if not name:
        return None
    lower = {b.lower(): b for b in known}
    return BRAND_ALIASES.get(name.lower()) or lower.get(name.lower()) or name.strip()


def guess_event_type(venue_type, supplier, venue):
    if venue_type in VENUE_EVENT_TYPES:
        return VENUE_EVENT_TYPES[venue_type]
    if FITNESS_WORDS.search(f"{supplier} {venue}"):
        return "Fitness Studios"
    return "Other"


def guess_category(cats):
    for label, rx in CATEGORY_TAGS:
        if cats and rx.search(cats):
            return label
    return "Food"


def guess_advertiser(brand, sponsor, advertisers):
    if brand in advertisers:
        return advertisers[brand]
    if brand in BRAND_ADVERTISERS:
        return BRAND_ADVERTISERS[brand]
    s = (sponsor or "").strip()
    return SPONSOR_ADVERTISERS.get(s.lower()) or re.sub(r",?\s+(LLC|Inc\.?|Co\.?|Corporation)$", "", s) or brand


def build_candidates(args):
    page = read_page()
    on_page = page_post_keys(page)
    blocks = page_brands(page)
    known = page_all_brands(page)
    advertisers = page_advertisers(page)
    skipped = set() if args.include_skipped else set(load_json(SKIPPED, []))

    tool_posts = fetch_photo_tool_posts()
    starred = {post_key(p["photo_url"]): p for p in tool_posts if p.get("photo_url") and post_key(p["photo_url"])}
    recaps = fetch_recaps(args.since, {p["recap_id"] for p in starred.values() if p.get("recap_id")})

    wanted = {canonical_brand(b, known).lower() for b in args.brand} if args.brand else None
    by_recap, seen = {}, set()
    for r in recaps:
        brand = canonical_brand(r["brand"], known)
        if not brand or brand.lower() == "test brand" or (wanted and brand.lower() not in wanted):
            continue
        # Already featuring this recap (often the IG twin of a FB cross-post): skip the rest of it.
        if any(post_key(u) in on_page for u in r["urls"]):
            continue
        for raw in r["urls"]:
            k = post_key(raw)
            if not k or k in on_page or k in skipped or k in seen:
                continue
            seen.add(k)
            url = clean_url(raw)
            c = {
                "key": k,
                "url": url,
                "embed": embed_src(url),
                "platform": platform_of(url),
                "brand": brand,
                "recap_brand": r["brand"],
                "brand_on_page": brand in known,
                "advertiser": guess_advertiser(brand, r["sponsor"], advertisers),
                "category": blocks.get(brand) or known.get(brand) or guess_category(r["cats"]),
                "event_type": guess_event_type(r["venue_type"], r["supplier"], r["venue"]),
                "supplier": r["supplier"] or "",
                "venue": r["venue"] or "",
                "event_date": r["start_date"].date().isoformat() if r["start_date"] else "",
                "recap_id": r["_id"],
                "starred": k in starred,
                "new_venue": html.escape(r["supplier"] or "") not in page,
            }
            by_recap.setdefault(r["_id"], []).append(c)

    candidates = []
    for posts in by_recap.values():
        # A venue usually cross-posts the same moment to IG and FB; offer the IG one.
        posts.sort(key=lambda c: c["platform"] != "instagram")
        candidates.append(posts[0])
    for c in candidates:
        rights = brand_rights(c["recap_brand"])
        c["rights"] = rights
        c["rights_limited"] = rights.lower().startswith("limited")
    candidates.sort(key=lambda c: (c["brand"].lower(), not c["starred"], not c["new_venue"], c["event_date"]), reverse=False)
    return candidates


# ---------- applying decisions ----------

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def card_html(c, small):
    e = lambda s: html.escape(s, quote=True)
    platform = "Instagram" if c["platform"] == "instagram" else "Facebook"
    mod = "ig" if c["platform"] == "instagram" else "fb"
    badge = f'<span class="post-card__brand">{e(c["brand"])}</span>' if small else ""
    return f'''
        <article class="post-card{' post-card--small' if small else ''}"
                 data-brand="{e(c['brand'])}"
                 data-category="{e(c['category'])}"
                 data-event-type="{e(c['event_type'])}">
          {badge}
          <header class="post-card__head">
            <span class="post-card__supplier">{e(c['supplier'])}</span>
            <span class="post-card__platform post-card__platform--{mod}">{platform}</span>
          </header>
          <div class="post-card__embed">
            <iframe src="{e(c['embed'])}"
                    loading="lazy"
                    frameborder="0"
                    scrolling="no"
                    allowtransparency="true"
                    allow="encrypted-media; autoplay"
                    title="{platform} post by {e(c['supplier'])}"></iframe>
          </div>
        </article>'''


def matching_close(page, start):
    """Index of the </div> that closes the <div ...> opening at `start`."""
    depth = 0
    for m in re.compile(r"<div\b|</div>").finditer(page, start):
        depth += 1 if m.group(0) == "<div" else -1
        if depth == 0:
            return m.start()
    raise ValueError("unbalanced divs")


def insert_card(page, c, blocks):
    esc = re.escape(html.escape(c["brand"], quote=True))
    if c["brand"] in blocks:
        m = re.search(rf'<div class="brand-block[^"]*"[^>]*data-brand="{esc}"[^>]*>', page)
        grid = page.index('<div class="post-grid">', m.end())
        small = False
    else:
        grid = page.index('<div class="showcase-grid">')
        small = True
    end = matching_close(page, grid)
    end = len(page[:end].rstrip())  # insert after the last card, before the closing indent
    return page[:end] + card_html(c, small) + page[end:]


def add_filter_entries(page, c):
    """New brands need a Brand dropdown option and an Advertiser mapping."""
    esc = html.escape(c["brand"], quote=True)
    sel = re.search(r'<select class="filter-select" data-group="brand">.*?</select>', page, re.S)
    if f'<option value="{esc}">' not in sel.group(0):
        new_sel = sel.group(0).replace("</select>", f'<option value="{esc}">{esc}</option></select>')
        page = page[:sel.start()] + new_sel + page[sel.end():]
    adv = re.search(r"var ADVERTISERS = \{(.*?)\n(\s*)\};", page, re.S)
    if f'"{c["brand"]}":' not in adv.group(1):
        entry = f',\n{adv.group(2)}  "{c["brand"]}": "{c["advertiser"]}"'
        page = page[:adv.end(1)] + entry + page[adv.end(1):]
    return page


def apply_decisions(decisions):
    page = read_page()
    blocks = page_brands(page)
    approved = decisions.get("approved", [])
    for c in approved:
        page = add_filter_entries(page, c)
        page = insert_card(page, c, blocks)
    with open(INDEX, "w", encoding="utf-8") as f:
        f.write(page)

    if approved:
        catalog = load_json(CATALOG, [])
        for c in approved:
            catalog.append({
                "brand_api": c["recap_brand"],
                "brand_display": c["brand"],
                "category": c["category"],
                "event_type": c["event_type"],
                "supplier": c["supplier"],
                "venue": c["venue"],
                "campaign": c["brand"],
                "platform": c["platform"],
                "url": c["url"],
                "recap_id": c["recap_id"],
            })
        with open(CATALOG, "w", encoding="utf-8") as f:
            json.dump(catalog, f, indent=2)
            f.write("\n")

    skipped = load_json(SKIPPED, [])
    skipped += [k for k in decisions.get("skipped", []) if k not in skipped]
    with open(SKIPPED, "w", encoding="utf-8") as f:
        json.dump(sorted(skipped), f, indent=2)
        f.write("\n")
    return len(approved), len(decisions.get("skipped", []))


# ---------- review server ----------

def serve_review(candidates, port, open_browser=True):
    with open(REVIEW_TEMPLATE, encoding="utf-8") as f:
        body = f.read().replace("/*__DATA__*/null", json.dumps({
            "candidates": candidates, "categories": CATEGORIES, "eventTypes": EVENT_TYPES,
        }).replace("</", "<\\/")).encode("utf-8")
    done = threading.Event()
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            decisions = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            added, skipped = apply_decisions(decisions)
            result.update(added=added, skipped=skipped)
            msg = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(msg)
            done.set()

    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"Review page: {url}  (tick posts to add, then Save)")
    if open_browser:
        webbrowser.open(url)
    try:
        done.wait()
    except KeyboardInterrupt:
        print("\nStopped without saving.")
        return
    server.shutdown()
    print(f"Added {result['added']} post(s); skipped {result['skipped']}.")
    print("Next: preview index.html, then commit and push to publish.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default=(dt.date.today() - dt.timedelta(days=180)).isoformat(),
                    help="only recaps with events on/after this date (default: 180 days ago)")
    ap.add_argument("--brand", action="append", help="limit to a brand (repeatable)")
    ap.add_argument("--include-skipped", action="store_true")
    ap.add_argument("--list", action="store_true", help="print candidates and exit")
    ap.add_argument("--apply", metavar="DECISIONS_JSON", help="apply a saved decisions file")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--no-browser", action="store_true", help="don't open the review page automatically")
    args = ap.parse_args()

    if args.apply:
        added, skipped = apply_decisions(load_json(args.apply, {}))
        print(f"Added {added} post(s); skipped {skipped}.")
        return

    candidates = build_candidates(args)
    if not candidates:
        print("No new posts found.")
        return
    if args.list:
        for c in candidates:
            flags = " ".join(f for f, on in [("STARRED", c["starred"]), ("NEW-VENUE", c["new_venue"]),
                                              ("NEW-BRAND", not c["brand_on_page"]),
                                              ("RIGHTS-LIMITED", c["rights_limited"])] if on)
            print(f'{c["brand"]:<22} {c["event_date"]}  {c["platform"]:<9} {c["supplier"][:32]:<32} {flags}\n    {c["url"]}')
        print(f"\n{len(candidates)} candidate(s).")
        return
    serve_review(candidates, args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    sys.exit(main())
