# Adding new social posts

`refresh.py` finds venue social posts that aren't on the page yet and lets you pick which to add.

```bash
python3 tools/refresh.py                              # last 180 days, every brand
python3 tools/refresh.py --brand Midol --brand Dove   # specific brands
python3 tools/refresh.py --since 2026-01-01           # wider date range
python3 tools/refresh.py --list                       # just print what it found
```

## Where posts come from

1. **Recap submissions:** the Instagram/Facebook links venues paste into their campaign recaps (BigQuery `mongodb.recaps`).
2. **Photo tool:** posts starred in the Recap Media Browser (`/social_posts`).

Posts already on the page are left out. So is the rest of a recap that's already featured (venues usually cross-post the same moment to IG and FB). TikTok links are ignored because the page has no TikTok embeds.

## Reviewing

A local review page opens with every candidate rendered the way the site shows it, grouped by brand. Facebook and Instagram block automated checks, so this is how dead posts get caught: they show as "unavailable" or blank.

- Tick the posts to add. Adjust event type if the guess is off.
- New brands land in "More brands on the platform". Set their category and advertiser, which feed the filters.
- Tags: **Starred in photo tool**, **New venue** (venue not on the page yet), **Rights limited** / **Rights not set** (from the brand's photo licensing in the photo tool; check before adding).
- **Save** writes approved posts into `index.html` and `_catalog.json`. Unticked posts go into `tools/skipped.json` so they don't come back (`--include-skipped` brings them back).

Then preview `index.html`, commit, and push to publish.

## Needs

- Python 3 with `google-cloud-bigquery`
- The Recess BigQuery service account key at `~/.claude/recess-tools/credentials/bigquery-service-account.json` (or set `RECESS_BQ_KEY`)

## When names don't line up

Recap brand names don't always match the page (`Kraft Macaroni & Cheese` vs `Kraft Mac & Cheese`). Add mappings to `BRAND_ALIASES` in `refresh.py`. For a brand whose recap sponsor is an agency or retailer (Mars United, Barrows, Walmart), set its real parent in `BRAND_ADVERTISERS`.
