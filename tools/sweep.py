#!/usr/bin/env python3
"""
ATS sweep for Bridget's search.

Reads targets.json, hits each employer's ATS job feed directly, filters on
title keywords, and writes ../jobs.json for the web board (plus results.md and
results.csv here in tools/ for eyeballing).

Same method as the crypto sweep, but the employer set here is on Workday and
iCIMS rather than Greenhouse/Lever/Ashby, so Workday is the one that matters.

Usage:
    python sweep.py                    # all targets
    python sweep.py --branch uxr       # one branch only
    python sweep.py --all-titles       # skip keyword filter, dump everything

Stdlib only, no pip install.
"""

import argparse
from datetime import datetime, timezone
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent          # repo root: where the deployable site lives
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

# Title keywords. Weighted: a hit in TIER1 ranks above a hit in TIER2.
# Tune these once the master's field is known.
TIER1 = [
    "research operations", "researchops", "research ops",
    "ux research", "user research", "user experience research",
    "research coordinator", "research associate", "research analyst",
    "survey research", "behavioral research", "behavioural research",
    "behavioral scien", "behavioural scien", "decision scien",
    "consumer insight", "human factors", "research program manager",
    "participant", "panel manager",
]
TIER2 = [
    "research assistant", "research specialist", "research manager",
    "survey", "usability", "insights", "evaluation",
    "study coordinator", "lab manager", "qualitative", "quantitative",
    "psychometric", "data collection", "experimental",
]

# Titles that match a keyword but are never the right role. Two groups:
# wrong discipline (the "research" in the title is engineering or bench science),
# and wrong seniority (well above a first move out of a coordinator seat).
EXCLUDE = [
    # wrong discipline
    "engineer", "engineering", "software", "developer", "machine learning",
    "ai research", "data scientist", "toxicolog", "radiation", "physiolog",
    "biomedical", "chemist", "vessel", "captain", "physician", "nurse",
    "pharmacist", "security", "clearance", "cleared", "top secret",
    # wrong track
    "faculty", "professor", "postdoc", "post-doc", "post doc", "adjunct",
    "intern", "phd student", "graduate assistant", "principal investigator",
    # wrong seniority
    "principal", "director", "vp ", "vice president", "head of", "chief",
    "distinguished", "fellow,", "executive",
]


def fetch(url, data=None, headers=None, timeout=30):
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ---------------------------------------------------------------- ATS parsers

def detect(careers_url):
    """Work out which ATS a careers URL belongs to, and pull the identifiers."""
    u = urllib.parse.urlparse(careers_url)
    host, path = u.netloc.lower(), u.path.strip("/")
    parts = [p for p in path.split("/") if p]

    if "myworkdayjobs.com" in host:
        # https://{tenant}.wd{N}.myworkdayjobs.com/[en-US/]{site}
        tenant = host.split(".")[0]
        # drop a locale segment if present (en-US, en_US, fr-CA, ...)
        segs = [p for p in parts if not re.fullmatch(r"[a-z]{2}[-_][A-Za-z]{2}", p)]
        if not segs:
            return None
        return {"ats": "workday", "host": host, "tenant": tenant, "site": segs[0]}

    if "greenhouse.io" in host and parts:
        return {"ats": "greenhouse", "token": parts[0]}

    if "lever.co" in host and parts:
        return {"ats": "lever", "token": parts[0]}

    if "ashbyhq.com" in host and parts:
        return {"ats": "ashby", "token": parts[0]}

    if careers_url.endswith(".atom") or "peopleadmin.com" in host:
        return {"ats": "peopleadmin", "host": host, "url": careers_url}

    if "icims.com" in host:
        return {"ats": "icims", "host": host, "url": careers_url}

    return None


# Server-side search terms for Workday. Paging an entire tenant does not scale
# (Leidos alone posts ~2k roles), and searchText filters server-side, so a
# handful of targeted queries beats walking the whole board.
SEARCH_TERMS = [
    "research operations", "user research", "ux research",
    "research coordinator", "research associate", "research analyst",
    "behavioral", "human factors", "survey", "insights",
]
MAX_PAGES = 10  # per search term, 20 per page


def pull_workday(t):
    """POST /wday/cxs/{tenant}/{site}/jobs, once per search term.
    limit maxes out at 20; asking for more returns an empty array with no error.
    Workday throttles fast paging, so pause between pages and retry once."""
    url = f"https://{t['host']}/wday/cxs/{t['tenant']}/{t['site']}/jobs"
    seen, out = set(), []

    for term in SEARCH_TERMS:
        offset, total = 0, None
        for _ in range(MAX_PAGES):
            payload = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term}
            try:
                d = fetch(url, data=payload)
            except Exception as e:
                print(f"    ! '{term}' offset {offset} failed ({e}), retrying", file=sys.stderr)
                time.sleep(3)
                try:
                    d = fetch(url, data=payload)
                except Exception as e2:
                    print(f"    ! gave up on '{term}': {e2}", file=sys.stderr)
                    break
            if total is None:
                total = d.get("total", 0)
            posts = d.get("jobPostings", [])
            if not posts:
                break
            for p in posts:
                ext = p.get("externalPath", "")
                if ext in seen:
                    continue
                seen.add(ext)
                out.append({
                    "title": p.get("title", ""),
                    "location": p.get("locationsText", ""),
                    "url": f"https://{t['host']}{ext}" if ext else "",
                    "posted": p.get("postedOn", ""),
                })
            offset += 20
            if total and offset >= total:
                break
            time.sleep(0.6)
        time.sleep(0.3)
    return out


def pull_greenhouse(t):
    d = fetch(f"https://boards-api.greenhouse.io/v1/boards/{t['token']}/jobs")
    return [{"title": j.get("title", ""),
             "location": (j.get("location") or {}).get("name", ""),
             "url": j.get("absolute_url", ""),
             "posted": j.get("updated_at", "")} for j in d.get("jobs", [])]


def pull_lever(t):
    d = fetch(f"https://api.lever.co/v0/postings/{t['token']}?mode=json")
    return [{"title": j.get("text", ""),
             "location": (j.get("categories") or {}).get("location", ""),
             "url": j.get("hostedUrl", ""),
             "posted": ""} for j in d]


def pull_ashby(t):
    d = fetch(f"https://api.ashbyhq.com/posting-api/job-board/{t['token']}")
    return [{"title": j.get("title", ""),
             "location": j.get("location", ""),
             "url": j.get("jobUrl", ""),
             "posted": j.get("publishedAt", "")} for j in d.get("jobs", [])]


def pull_peopleadmin(t):
    """PeopleAdmin (jobs.<university>.edu) publishes an Atom feed at
    /postings/search.atom. Entries carry no location field, only a department in
    <author>, so the campus location comes from the target entry instead."""
    import xml.etree.ElementTree as ET
    ns = {"a": "http://www.w3.org/2005/Atom"}
    req = urllib.request.Request(t["url"], headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        root = ET.fromstring(r.read())

    out = []
    for e in root.findall("a:entry", ns):
        title = (e.findtext("a:title", "", ns) or "").strip()
        link = e.find("a:link", ns)
        dept = (e.findtext("a:author/a:name", "", ns) or "").strip()
        out.append({
            "title": title,
            "location": t.get("location") or dept,
            "url": (link.get("href") if link is not None else "") or e.findtext("a:id", "", ns),
            "posted": (e.findtext("a:published", "", ns) or "")[:10],
        })
    return out


def _html(url, timeout=25):
    h = {"User-Agent": UA,
         "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
         "Accept-Language": "en-US,en;q=0.9"}
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def pull_eightfold(t):
    """Eightfold AI (e.g. Johns Hopkins). The documented /api/apply/v2/jobs route
    403s; the one the career site actually calls is /api/pcsx/search, and it needs
    a Referer. Results live under data.positions, 10 per page, paged via start."""
    base = t["url"]
    ref = base.split("/api/")[0] + "/careers"
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Referer": ref}
    out, start, total = [], 0, None
    while start < 500:
        sep = "&" if "?" in base else "?"
        u = f"{base}{sep}query=&location=&start={start}&"
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers=h), timeout=25) as r:
                d = json.loads(r.read()).get("data", {})
        except Exception:
            break
        pos = d.get("positions") or []
        if total is None:
            total = d.get("count", 0)
        if not pos:
            break
        for p in pos:
            locs = p.get("locations") or []
            out.append({
                "title": p.get("name", ""),
                "location": ", ".join(locs[:2]) if locs else "",
                "url": f"{ref}/job/{p.get('id')}",
                "posted": "",
            })
        start += len(pos)
        if total and start >= total:
            break
        time.sleep(0.4)
    return out


def pull_htmljobs(t):
    """Career sites that render job links server-side (SAP SuccessFactors at Duke,
    Harvard's bespoke site). Pull hrefs matching the target's job-link pattern.
    Brittle by nature: if a school redesigns, its rule here needs updating."""
    rx = re.compile(t["job_re"], re.I)
    seen, out = set(), []
    for page in range(t.get("pages", 1)):
        u = t["url"].replace("{page}", str(page))
        try:
            html = _html(u)
        except Exception:
            break
        found = rx.findall(html)
        if not found:
            break
        for href, title in found:
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", title)).strip()
            if not title or title.lower() in ("read more", "learn more", "view job"):
                continue
            full = href if href.startswith("http") else t["base"] + href
            if full in seen:
                continue
            seen.add(full)
            out.append({"title": title, "location": t.get("location", ""),
                        "url": full, "posted": ""})
        time.sleep(0.4)
    return out


PULLERS = {
    "workday": pull_workday,
    "greenhouse": pull_greenhouse,
    "lever": pull_lever,
    "ashby": pull_ashby,
    "peopleadmin": pull_peopleadmin,
    "eightfold": pull_eightfold,
    "htmljobs": pull_htmljobs,
}


# ------------------------------------------------------------------- scoring

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "Washington DC",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
NAME_TO_CODE = {v.lower(): k for k, v in US_STATES.items()}
NAME_TO_CODE["washington, d.c."] = "DC"
NAME_TO_CODE["washington d.c."] = "DC"

_STATE_RX = re.compile(r"(?:^|[\s,(|\-])(" + "|".join(US_STATES) + r")(?=[\s,)|]|$)")
_NONUS_RX = re.compile(
    r"canada|mexico|united kingdom|london|germany|belgium|brussels|spain|madrid|portugal|"
    r"india|bangalore|japan|tokyo|ireland|dublin|france|paris|netherlands|amsterdam|"
    r"australia|singapore|brazil|poland|sweden|israel|switzerland|italy|korea|china|"
    r"philippines|ontario|quebec|toronto", re.I)


# Metro areas, checked before falling back to a bare state. A state is a poor
# filter here ("Maryland" spans Baltimore and the DC suburbs, which are different
# commutes); a metro is what someone actually searches on.
METROS = [
    ("DC", "Washington DC metro", [
        "washington, d.c", "washington dc", "washington, dc", "us-dc", "-dc-",
        "arlington", "alexandria", "fairfax", "reston", "mclean", "falls church",
        "bethesda", "rockville", "silver spring", "college park", "gaithersburg",
        "chevy chase", "hyattsville", "manassas", "herndon", "vienna, va",
        "springfield, va", "annandale", "ashburn", "sterling, va"]),
    ("BAL", "Baltimore", ["baltimore", "towson", "catonsville", "columbia, md", "bel air"]),
    ("NYC", "New York City area", [
        "new york", "nyc", "manhattan", "brooklyn", "bronx", "newark", "jersey city",
        "new brunswick", "piscataway", "hoboken", "yonkers", "white plains", "queens, ny"]),
    ("BOS", "Boston area", [
        "boston", "cambridge", "medford", "somerville", "waltham", "chestnut hill",
        "brookline", "newton, ma", "quincy, ma", "charlestown, ma"]),
    ("PHL", "Philadelphia area", [
        "philadelphia", "villanova", "bryn mawr", "radnor", "wynnewood", "camden, nj"]),
    ("PIT", "Pittsburgh", ["pittsburgh"]),
    ("RDU", "Research Triangle NC", [
        "raleigh", "durham", "chapel hill", "cary, nc", "research triangle", "rtp"]),
    ("ATL", "Atlanta", ["atlanta", "decatur, ga", "athens, ga"]),
    ("MIA", "South Florida", ["miami", "coral gables", "fort lauderdale", "boca raton"]),
    ("TPA", "Tampa", ["tampa"]),
    ("RIC", "Richmond VA", ["richmond, va"]),
    ("CHO", "Charlottesville VA", ["charlottesville"]),
    ("WMB", "Williamsburg VA", ["williamsburg"]),
    ("BLK", "Blacksburg VA", ["blacksburg"]),
    ("CHS", "Charleston SC", ["charleston, sc"]),
    ("CAE", "Columbia SC", ["columbia, sc"]),
    ("CLE", "Clemson SC", ["clemson"]),
    ("WSL", "Winston-Salem NC", ["winston-salem", "winston salem"]),
    ("PVD", "Providence RI", ["providence"]),
    ("NHV", "New Haven CT", ["new haven"]),
    ("STR", "Storrs / Hartford CT", ["storrs", "hartford"]),
    ("ITH", "Ithaca NY", ["ithaca"]),
    ("ROC", "Rochester NY", ["rochester, ny"]),
    ("SYR", "Syracuse NY", ["syracuse"]),
    ("BUF", "Buffalo NY", ["buffalo"]),
    ("ALB", "Albany NY", ["albany, ny"]),
    ("BGM", "Binghamton NY", ["binghamton"]),
    ("BTV", "Burlington VT", ["burlington, vt"]),
    ("HAN", "Hanover NH", ["hanover, nh"]),
    ("AMH", "Amherst MA", ["amherst"]),
    ("SCE", "State College PA", ["university park, pa", "state college"]),
    ("BTH", "Bethlehem PA", ["bethlehem, pa"]),
    ("GNV", "Gainesville FL", ["gainesville"]),
    ("TLH", "Tallahassee FL", ["tallahassee"]),
    ("CHI", "Chicago", ["chicago"]),
    ("SFO", "SF Bay Area", [
        "san francisco", "palo alto", "mountain view", "oakland, ca", "berkeley",
        "san jose", "sunnyvale", "menlo park"]),
    ("SEA", "Seattle", ["seattle", "bellevue, wa", "redmond, wa"]),
    ("LAX", "Los Angeles", ["los angeles", "santa monica", "pasadena"]),
    ("AUS", "Austin TX", ["austin"]),
    ("DEN", "Denver / Boulder", ["denver", "boulder"]),
]


def classify_metros(*texts):
    """Return every metro a posting touches. Multi-site strings like
    'US-Remote | US-VA-Arlington | US-NC-Chapel Hill' legitimately match several."""
    blob = " ".join(t for t in texts if t).lower()
    out = []
    for code, _label, keys in METROS:
        if any(k in blob for k in keys) and code not in out:
            out.append(code)
    return out


def normalize_location(raw, fallback=""):
    """Feeds hand back wildly inconsistent location strings: real cities, but also
    building names ("164 Angell Street", "Medical Center") and department labels.
    Fall back to the employer's campus city when the raw value has no usable
    geography, so the location filter has something real to work with.

    Returns (display, [state codes], remote, non_us)."""
    raw = (raw or "").strip()
    remote = bool(re.search(r"\bremote\b|telework|work from home", raw, re.I))
    non_us = bool(_NONUS_RX.search(raw))

    states = []
    for m in _STATE_RX.finditer(raw.upper()):
        if m.group(1) not in states:
            states.append(m.group(1))
    if not states:
        low = raw.lower()
        for nm, code in NAME_TO_CODE.items():
            if nm in low and code not in states:
                states.append(code)

    display = raw
    # No geography at all (a building or department) -> use the campus city.
    if not states and not remote and not non_us and fallback:
        display = fallback
        for m in _STATE_RX.finditer(fallback.upper()):
            if m.group(1) not in states:
                states.append(m.group(1))

    if remote and not states:
        states = ["REMOTE"]
    elif remote:
        states = ["REMOTE"] + states

    if not states:
        states = ["INTL"] if non_us else ["OTHER"]

    return display or fallback or "Location not listed", states, remote, non_us


def score(title):
    low = title.lower()
    if any(x in low for x in EXCLUDE):
        return 0
    if any(k in low for k in TIER1):
        return 2
    if any(k in low for k in TIER2):
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--branch", help="only sweep targets in this branch")
    ap.add_argument("--all-titles", action="store_true", help="skip the keyword filter")
    args = ap.parse_args()

    targets = json.loads((HERE / "targets.json").read_text(encoding="utf-8"))
    if args.branch:
        targets = [t for t in targets if t.get("branch") == args.branch]

    rows, skipped = [], []
    for t in targets:
        name, careers = t["name"], t.get("careers_url", "").strip()
        if not careers:
            skipped.append((name, "no careers_url yet"))
            continue
        # A target may name its platform explicitly, for the ones whose URL shape
        # is not self-describing (eightfold, hand-written HTML rules).
        if t.get("ats"):
            info = {"ats": t["ats"], "url": careers, **{k: v for k, v in t.items()
                                                        if k in ("job_re", "base", "pages")}}
        else:
            info = detect(careers)
        if not info:
            skipped.append((name, f"unrecognized ATS: {careers}"))
            continue
        if info["ats"] not in PULLERS:
            # iCIMS has no clean public JSON feed; these need the browser.
            skipped.append((name, f"{info['ats']} needs manual/browser check"))
            continue

        print(f"  {name} ({info['ats']}) ...", flush=True)
        info["location"] = t.get("location", "")
        try:
            jobs = PULLERS[info["ats"]](info)
        except Exception as e:
            skipped.append((name, f"fetch failed: {e}"))
            continue

        kept = 0
        for j in jobs:
            s = 3 if args.all_titles else score(j["title"])
            if s == 0:
                continue
            kept += 1
            disp, states, remote, non_us = normalize_location(
                j["location"], t.get("location", ""))
            metros = [] if non_us else classify_metros(j["location"], disp)
            if remote:
                metros = ["REMOTE"] + metros
            if not metros:
                metros = ["INTL"] if non_us else ["USOTHER"]
            rows.append({"score": s, "employer": name, "branch": t.get("branch", ""),
                         "title": j["title"], "location": disp,
                         "states": states, "metros": metros,
                         "remote": remote, "intl": non_us,
                         "posted": j["posted"], "url": j["url"]})
        print(f"    {kept} hits of {len(jobs)} postings")
        time.sleep(0.4)

    # Big research universities post the same requisition many times over, one
    # per department (Miami had 125 rows, mostly "Clinical Research Coordinator 1").
    # Collapse identical employer+title into one row carrying a count, so a single
    # employer cannot bury the rest of the board.
    grouped = {}
    for r in rows:
        key = (r["employer"], r["title"].strip().lower())
        if key in grouped:
            g = grouped[key]
            g["count"] += 1
            if r["location"] and r["location"] not in g["locations"]:
                g["locations"].append(r["location"])
            for st in r["states"]:
                if st not in g["states"]:
                    g["states"].append(st)
            for mt in r["metros"]:
                if mt not in g["metros"]:
                    g["metros"].append(mt)
            g["remote"] = g["remote"] or r["remote"]
        else:
            grouped[key] = {**r, "count": 1, "states": list(r["states"]),
                            "metros": list(r["metros"]),
                            "locations": [r["location"]] if r["location"] else []}

    rows = []
    for g in grouped.values():
        locs = g.pop("locations")
        if g["count"] > 1 and len(locs) > 1:
            g["location"] = f"{locs[0]} +{len(locs) - 1} more"
        rows.append(g)

    rows.sort(key=lambda r: (-r["score"], r["employer"], r["title"]))

    with (HERE / "results.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["score", "employer", "branch", "title",
                                          "location", "posted", "url", "count"],
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    lines = [f"# Sweep results ({len(rows)} hits across {len(targets)} targets)", ""]
    for tier, label in ((2, "Strong title match"), (1, "Worth a look"), (3, "All titles")):
        block = [r for r in rows if r["score"] == tier]
        if not block:
            continue
        lines += [f"## {label}", "", "| Employer | Title | Location | Link |",
                  "|---|---|---|---|"]
        for r in block:
            lines.append(f"| {r['employer']} | {r['title']} | {r['location']} | [link]({r['url']}) |")
        lines.append("")
    if skipped:
        lines += ["## Skipped", ""]
        lines += [f"- **{n}** - {why}" for n, why in skipped]
    (HERE / "results.md").write_text("\n".join(lines), encoding="utf-8")

    # Feed the web board. The page reads this at load, so re-running the sweep
    # is all it takes to refresh what she sees. Same static-data-pipeline shape
    # as the dashboards: collector writes JSON, the page renders it.
    if True:
        # Location facet the page turns into a dropdown, ordered by how many
        # rows each place has so the useful options sit at the top.
        counts = {}
        for r in rows:
            for mt in r["metros"]:
                counts[mt] = counts.get(mt, 0) + 1
        label = {c: l for c, l, _ in METROS}
        label.update(REMOTE="Remote", INTL="Outside the US",
                     USOTHER="Elsewhere in the US")
        # Remote first, then by size; the catch-alls sink to the bottom.
        def rank(kv):
            c, n = kv
            return (0 if c == "REMOTE" else 2 if c in ("USOTHER", "INTL") else 1, -n, c)
        facet = [{"code": c, "label": label.get(c, c), "n": n}
                 for c, n in sorted(counts.items(), key=rank)]

        payload = {
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "locations": facet,
            "swept": sum(1 for t in targets if t.get("careers_url")),
            "targets": len(targets),
            "manual": [
                {"name": t["name"], "branch": t.get("branch", ""), "portal": t["portal"]}
                for t in targets if not t.get("careers_url") and t.get("portal")
            ],
            "jobs": rows,
        }
        (ROOT / "jobs.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
        print(f"  wrote ../jobs.json ({len(rows)} jobs)")

    print(f"\n{len(rows)} hits -> results.md / results.csv")
    if skipped:
        print(f"{len(skipped)} targets skipped (see results.md)")


if __name__ == "__main__":
    main()
