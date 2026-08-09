# bridget-jobs

A job board for research roles: open positions pulled directly from employer ATS
feeds, filterable by lane, metro area and remote status, plus a ranked guide to
where to look manually.

Static site, no backend, no build step.

## Layout

```
index.html      the whole site (inline CSS + JS, no dependencies)
jobs.json       generated data the page fetches at load
vercel.json     noindex headers
tools/
  sweep.py      pulls every employer feed -> writes ../jobs.json
  targets.json  the employer list (76 employers, 56 with machine-readable feeds)
  discover_ats.py  helper for working out which ATS a new employer uses
docs/
  where-to-look.md   the ranked source guide, same content as the second tab
```

## Refreshing the data

```bash
cd tools
python sweep.py        # stdlib only, no pip install
```

Takes a few minutes; most of it is Workday tenants, which cap at 20 results per
request and throttle fast paging. It rewrites `../jobs.json`, then redeploy.

**The deployed board is a snapshot, not a live feed.** Postings close. Re-running
the sweep and redeploying is the only thing that updates it.

## Deploying

```bash
vercel --prod
```

Zero config: Vercel detects a static site, serves `index.html` from the root, and
`jobs.json` sits beside it. Nothing to build.

Note the URL is unlisted, not private. `vercel.json` sets `X-Robots-Tag: noindex,
nofollow` so it stays out of search engines, but anyone with the link can open it.

## Adding an employer

Add an entry to `tools/targets.json`:

```json
{ "branch": "university", "name": "Some University",
  "careers_url": "https://someuni.wd1.myworkdayjobs.com/Careers",
  "location": "City, ST" }
```

`careers_url` is auto-detected for Workday, Greenhouse, Lever, Ashby and
PeopleAdmin (`/postings/search.atom`). For anything else, set `"ats"` explicitly
to `eightfold` or `htmljobs` — see the Johns Hopkins, Duke and Harvard entries for
worked examples of each.

`location` is the campus city, used as a fallback when a feed returns a building
name instead of geography (Brown reports "164 Angell Street", Georgetown reports
"Medical Center").

To find a new employer's feed, `python tools/discover_ats.py` crawls HR pages and
extracts ATS links.

## Known limits

- **20 of 76 employers have no machine-readable feed** and are listed in the
  "Check these by hand" section on the page. Several are strong-fit employers
  (Westat, Mathematica, NORC, Abt), so that section is not optional.
- Big research universities post the same requisition many times over, once per
  department. Identical employer+title rows are collapsed into one row carrying an
  "N openings" badge.
- The `htmljobs` adapter is regex over someone else's markup. It will break when
  those sites are redesigned.
