# bridget-jobs

A job board for research roles: open positions pulled directly from employer ATS
feeds, filterable by lane, metro area, remote status, seniority and pay,
previewable in place, plus a ranked guide to where to look manually.

Static site, no backend, no build step.

## Layout

```
index.html      the whole site (inline CSS + JS, no dependencies)
jobs.json       generated data the page fetches at load
vercel.json     noindex headers
tools/
  sweep.py      pulls every employer feed -> writes ../jobs.json
  targets.json  the employer list (88 employers, 76 with machine-readable feeds)
  discover_ats.py  helper for working out which ATS a new employer uses
docs/
  where-to-look.md   the ranked source guide, same content as the second tab
```

## Reading a role without leaving the page

The board is a list beside a preview pane. Clicking a card opens the posting in
the pane instead of navigating away; the pane holds the title, employer,
location, age, the same badges the card carries, an **Open the posting** link,
the Applied / Hide marks, and the posting text where the feed publishes one.
Arrow keys walk the list, so a whole filtered set can be skimmed without the
mouse. Ctrl-click, middle-click and "open in new tab" still work, because the
card is a real link underneath.

Below 900px there is nowhere to put a pane, so the board falls back to the plain
list it was before and cards open their posting directly.

`sweep.py` fills a `desc` field per row. Greenhouse, PeopleAdmin, Ashby, Lever,
Jibe and Amazon all hand over the posting text in the same request as the
listing, so those cost nothing. Workday and Eightfold do not, and get one extra
request per row from `enrich_bodies()`, which runs *after* the title filter and
*after* grouping and reuses the previous sweep's result for rows it already
knows, so a daily refresh pays only for genuinely new postings. The rest
(the scraped HTML boards, Radancy, ADP, Workable, BambooHR, UKG, Oracle) publish
no body worth having; those rows say so in the pane rather than showing a blank
panel. Text is truncated to 1,600 characters and rendered as text, never as
markup.

## Seniority and pay

Two filters that no feed supports directly, so the sweep derives both.

**Seniority** (`level`) is read off the title by `level_of()`, first matching
rule wins: an explicit word (senior, sr., lead, manager) beats a level number,
and a level number beats the role word, so "Sr. Research Associate 1" is senior
and "Research Assistant 2" is not entry. Roman numerals are matched
case-sensitively, since a lowercased `i` appears in half the titles on the
board. A title with no marker at all stays blank rather than being filed under a
level it might not belong to, so it drops out when a level is selected.

**Pay** (`pay`) is read out of the posting body by `find_pay()`. US pay
transparency means most of these postings state a range, but in the last
paragraph, precisely what the 1,600-character preview truncation discards, so
extraction runs on the full text before `trim()`. A range counts only if a
pay word sits within 80 characters of it and no disqualifying word does
(`bonus`, `tuition`, `grant`, `budget`, `relocation`, …), which is what keeps a
signing bonus or a grant figure out of the salary field. Magnitude alone
decides hourly versus annual (nobody earns $23 a year or $87,000 an hour), and
matching on "/hr" would also match the "HR" in half these titles. Hourly rates
are annualised at 2,080 hours so a single filter can compare them against
salaries; `min`/`max` are therefore always annual dollars while `label` keeps
the employer's own units. Ashby is the one feed that publishes pay as a real
field, so it is preferred over the prose there, and its non-dollar tiers are
ignored rather than sorted as if the numbers were comparable.

Nothing is ever guessed: a posting with no trustworthy range gets `null`, and
the pay filter treats unstated as failing, so every option below "Any pay" also
means "and the pay is published". The floor compares against the **top** of the
range, so `$100k+` asks "could this pay at least that", not "does it start
there".

## Refreshing the data

```bash
cd tools
python sweep.py        # stdlib only, no pip install
```

Takes a few minutes; most of it is Workday tenants, which cap at 20 results per
request and throttle fast paging. It rewrites `../jobs.json`, then redeploy.

**The deployed board is a snapshot, not a live feed.** Postings close. Re-running
the sweep and redeploying is the only thing that updates it.

### Automatic refresh

`.github/workflows/refresh.yml` runs the sweep daily at 11:00 UTC and commits
`jobs.json` when it changes, as `github-actions[bot]`. Run it on demand from the
Actions tab (**Refresh job board → Run workflow**).

It refuses to commit a result that looks broken (fewer than 40 jobs, less than
half the previous count, or under half the employers responding), so one bad
network day cannot replace a good board with an empty one. The run fails loudly
instead.

If the repo is linked to Vercel's GitHub integration, that commit redeploys the
site on its own. If you deploy from the CLI instead, create a Vercel Deploy Hook
and add it as a `VERCEL_DEPLOY_HOOK` repository secret; the last step picks it up.

## Deploying

```bash
vercel --prod
```

Zero config: Vercel detects a static site, serves `index.html` from the root, and
`jobs.json` sits beside it. Nothing to build.

Note the URL is unlisted, not private. `vercel.json` sets `X-Robots-Tag: noindex,
nofollow` so it stays out of search engines, but anyone with the link can open it.

## Social preview

`og.png` is generated, not hand-drawn, so its numbers stay true:

```bash
node tools/build-og.mjs      # reads jobs.json, renders og.png via headless Chrome
```

Re-run it after a big sweep if you want the card's counts to match the board.
No npm dependency; set `CHROME_PATH` if Chrome isn't found automatically.

The tab icon (`favicon-32.png`, `favicon-180.png`) is generated by the same
script, circle-cropped and zoomed on the face because the full head-and-shoulders
framing is unreadable at 32px.

**On `vercel.json`:** it is validated against a strict schema, so a header entry
may contain only `key` and `value`. Do not add a `comment` field: Vercel rejects
the whole deploy. The reason `X-Robots-Tag` is `noindex` and not `noindex,
nofollow` is that some social crawlers treat `nofollow` as a reason to skip
fetching the `og:` tags, which would kill the link preview.

**After the first deploy, check the domain.** `og:image` and `og:url` in
`index.html` are absolute (social crawlers require it) and currently assume
`https://bridget-jobs.vercel.app`. If Vercel assigns something else, update both.

Test the card with LinkedIn's [Post Inspector](https://www.linkedin.com/post-inspector/).
LinkedIn caches aggressively, so inspect before sharing rather than after.

## Adding an employer

Add an entry to `tools/targets.json`:

```json
{ "branch": "university", "name": "Some University",
  "careers_url": "https://someuni.wd1.myworkdayjobs.com/Careers",
  "location": "City, ST" }
```

`careers_url` is auto-detected for Workday, Greenhouse, Lever, Ashby, Workable
(`apply.workable.com/{account}`), Oracle Recruiting Cloud (a
`.../CandidateExperience/en/sites/{site}/requisitions` URL) and PeopleAdmin
(`/postings/search.atom`). For anything else, set `"ats"` explicitly to
`eightfold`, `htmljobs`, `amazonjobs`, `radancy` or `jibe`. See the Johns
Hopkins, Duke, Harvard, Amazon, Intuit and Emmes entries for worked examples.
BambooHR (`{company}.bamboohr.com`), UKG Pro (`recruiting.ultipro.com/...`)
and ADP WorkforceNow (a `workforcenow.adp.com/...?cid=...` URL) are also
auto-detected. An employer with no feed at all can still set `"watch_url"` to
get its careers page change-watched (see ideas42).

`location` is the campus city, used as a fallback when a feed returns a building
name instead of geography (Brown reports "164 Angell Street", Georgetown reports
"Medical Center").

To find a new employer's feed, `python tools/discover_ats.py` crawls HR pages and
extracts ATS links.

## Known limits

- **12 of 88 employers have no machine-readable feed** and are listed in the
  "Check these by hand" section on the page. Several are strong-fit employers
  (Westat, Fors Marsh, Mathematica, NORC), so that section is not optional.
  Westat's BrassRing SPA, NORC's Cloudflare, and Mathematica's
  headless-hostile Radancy build resist both plain fetches and rendered
  Chrome; Meta, Google, Microsoft and MDRC are custom/JS-only with no
  reachable feed. The per-entry `portal` notes in `targets.json` record
  exactly what was tried, so nobody re-derives a dead end. (Probed and
  rejected entirely, not even worth a manual entry: YouGov, Gallup, IQVIA,
  ICON, Parexel, Syneos, Ipsos, Circana, WCG, Axle, EnCompass, 2M Research,
  Atlas Research, Marvin, Suzy, Zappi, Discuss.io - JS shells, WAFs, or
  private boards.)
- **Boardless employers are watched, not scraped.** Targets with a `watch_url`
  (Fors Marsh, SSRS, ideas42, Irrational Labs, TRI, MDRC) get their careers
  page text hashed each sweep; when the hash moves, the board badges that
  entry "Page changed" for two weeks. A `render_html()` helper (headless
  Chrome `--dump-dom`, no dependencies, `render: true` on an `htmljobs`
  target) exists for WAF-fronted or JS-rendered boards, though every current
  candidate defeats it in its own way.
- **A source that errors mid-sweep keeps its previous rows.** Failed employers'
  listings are carried forward from the last `jobs.json` with `"stale": true`
  rather than vanishing, so one bad network day (or a WAF that hates the CI
  runner's IP) cannot silently shrink the board.
- Big research universities post the same requisition many times over, once per
  department. Identical employer+title rows are collapsed into one row carrying an
  "N openings" badge.
- The `htmljobs` adapter is regex over someone else's markup. It will break when
  those sites are redesigned.
- **Posting dates are best-effort.** Each card shows an age, the board can sort
  newest-first and filter to "New this week", but the date comes in whatever
  shape the source ATS uses: Workday sends relative strings (anchored on the
  sweep timestamp, and capped at "30+ days"), and the scraped HTML boards
  (Duke, Harvard) carry no date at all. The sweep also stamps every row with a
  `first_seen` date carried forward across runs, so date-blind sources still
  get an age ("first appeared on this board") once they have been through two
  sweeps; rows that predate the tracking stay undated until they close. "New"
  means new on this board, which also catches an old posting that enters the
  feed when an employer is added.
- **Seniority is inferred, not reported.** No feed publishes a level field, so
  `level_of()` reads it off the title. It is right on the titles that carry a
  marker and blank on the ones that do not, which means picking a level drops
  the unmarked rows rather than guessing at them.
- **Pay is only as good as the posting.** Roughly half of these employers state
  a range and the rest do not, so the pay filter is a filter on *published*
  pay. It also cannot see past the truncation on the feeds with no body at all
  (the scraped HTML boards, Radancy, ADP), which have no pay for the same
  reason they have no description.
- **Applied / Hide marks live in the browser**, in the same localStorage as the
  guide tab's checkboxes, keyed by employer+title so they survive re-sweeps.
  Hidden jobs collapse into an "N hidden" chip; nothing is ever deleted.
