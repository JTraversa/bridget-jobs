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
import hashlib
import html
import json
import os
import re
import subprocess
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
    "research scholar", "survey", "usability", "insights", "evaluation",
    "study coordinator", "lab manager", "qualitative", "quantitative",
    "psychometric", "data collection", "experimental",
    # lane C (clinical regulatory ops) - her TRI credential. Added alongside
    # the CRO feeds (Medpace, Advarra, Emmes, FNL); without these, titles like
    # "Regulatory Affairs Associate" or "Clinical Trial Assistant" never match.
    "clinical research", "clinical trial", "regulatory affairs",
    "regulatory operations", "trial master", "etmf", "clinical document",
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


# ------------------------------------------------------------- descriptions

DESC_LIMIT = 1600   # characters kept per posting


def plain_text(*parts):
    """ATS description HTML -> plain text, whole thing.

    Every feed sends a different flavour of markup (Greenhouse double-escapes
    its HTML, Amazon uses bare <br/>, Workday nests <p style=...>), so the
    approach is deliberately blunt: turn block-level tags into line breaks,
    drop the rest, unescape twice. The board renders the result as text, never
    as markup, so nothing here is load-bearing for safety - it is purely about
    readability.
    """
    raw = "\n\n".join(p for p in parts if p and str(p).strip())
    if not raw:
        return ""
    text = html.unescape(html.unescape(str(raw)))
    text = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", text, flags=re.I)
    text = re.sub(r"<\s*li[^>]*>", "\n- ", text, flags=re.I)
    text = re.sub(r"<\s*br[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"</\s*li\s*>", " ", text, flags=re.I)   # the opening <li> already broke the line
    text = re.sub(r"</\s*(p|div|ul|ol|h[1-6]|tr|table)\s*>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text).replace(" ", " ").replace("​", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n[ \n]*\n[ \n]*", "\n\n", text)   # collapse blank runs
    text = re.sub(r" *\n *", "\n", text).strip()
    return text


def trim(text, limit=DESC_LIMIT):
    """Cut to preview length on a sentence edge, then a line, then a word."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    edge = max(cut.rfind(". "), cut.rfind(".\n"), cut.rfind("\n"))
    if edge < limit * 0.6:
        edge = cut.rfind(" ")
    return cut[:edge].rstrip(" ,;:-") + "…"


# ---------------------------------------------------------------------- pay

# A dollar figure, optionally abbreviated ("$120K", "$1.2M").
_MONEY = r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*([KkMm])?"
_RANGE = re.compile(_MONEY + r"\s*(?:-|to)\s*" + _MONEY)
# Words that make a nearby range mean pay, and words that make it mean anything
# but ("a $2,500 signing bonus", "a $4.5 million grant", "$5,250 tuition").
_PAY_LABEL = re.compile(r"salary|pay|compensation|wage|hourly|rate|earn|hiring range", re.I)
_PAY_VETO = re.compile(r"bonus|401|tuition|reimburs|award|grant|budget|relocat|equity|"
                       r"revenue|scholarship|portfolio|endowment|fee\b|fine|penalt", re.I)

HOURS_PER_YEAR = 2080   # 40h x 52w, the usual US full-time convention
HOURLY_CEILING = 250    # above this a figure is a salary, below it a rate


def _dollars(num, suffix):
    v = float(num.replace(",", ""))
    if suffix:
        v *= 1000 if suffix.lower() == "k" else 1_000_000
    return v


def _money_label(lo, hi, hourly):
    if hourly:
        # whole dollars stay whole, cents keep both digits ("$15.40", not "$15.4")
        fmt = lambda v: "%d" % v if v == int(v) else "%.2f" % v
        return "$%s - $%s/hr" % (fmt(lo), fmt(hi))
    k = lambda v: "$%gk" % round(v / 1000)
    return "%s - %s" % (k(lo), k(hi))


def find_pay(text, require_label=True):
    """Pull a stated pay range out of a full posting body.

    US pay-transparency laws mean most of these postings state a range, but
    almost always in the last paragraph - which is exactly the part the preview
    truncation throws away, so this has to run on the whole body before trim().

    Returns min/max as ANNUAL dollars so one filter can compare salaried and
    hourly postings, with the label kept in the units the employer actually
    wrote. None when nothing trustworthy is stated: a missing range is normal
    and must never be guessed at.
    """
    if not text:
        return None
    flat = re.sub(r"[‐-―−]", "-", text)
    best = None
    for m in _RANGE.finditer(flat):
        before = flat[max(0, m.start() - 80):m.start()]
        after = flat[m.end():m.end() + 40]
        if _PAY_VETO.search(before):
            continue
        lo, hi = _dollars(m.group(1), m.group(2)), _dollars(m.group(3), m.group(4))
        if hi < lo:
            continue
        # Magnitude decides hourly vs annual, not the surrounding words: nobody
        # earns $23 a year or $87,000 an hour, and matching on "/hr" would also
        # match the "HR" in half the job titles on this board.
        hourly = hi < HOURLY_CEILING
        a_lo, a_hi = (lo * HOURS_PER_YEAR, hi * HOURS_PER_YEAR) if hourly else (lo, hi)
        # Anything left outside this band is some other number that happened to
        # sit in a range: a $5,000 stipend, a $12m contract.
        if a_hi < 20_000 or a_lo > 1_500_000:
            continue
        score = (2 if _PAY_LABEL.search(before) else 0) + (1 if _PAY_LABEL.search(after) else 0)
        if best is None or score > best[0]:
            best = (score, {"min": int(a_lo), "max": int(a_hi),
                            "period": "hour" if hourly else "year",
                            "label": _money_label(lo, hi, hourly)})
        if best[0] >= 3:
            break
    # In prose an unlabelled range is a coincidence as often as it is pay, so
    # require one of the two windows to say what the number is. A field that
    # exists only to hold compensation needs no such proof.
    if not best or (require_label and best[0] == 0):
        return None
    return best[1]


def body(*parts):
    """The two things worth keeping from a posting body, ready to splat into a
    row: the preview text, and any pay range stated further down than it."""
    full = plain_text(*parts)
    return {"desc": trim(full), "pay": find_pay(full)}


# ------------------------------------------------------------------- levels

# Ordered: the first rule that matches a title wins. Explicit seniority words
# beat a level number ("Sr. Research Associate 1" is a senior role), and a level
# number beats the role word ("Research Assistant 2" is not entry level).
# Roman numerals must stay case-SENSITIVE: lowercased, the bare "i" and "v" in
# ordinary words would read as level markers everywhere.
LEVEL_RULES = [
    ("senior", re.compile(r"\b(senior|sr\.?|lead|principal|manager|supervisor|head of|chief)\b", re.I)),
    ("entry", re.compile(r"\b(intern|trainee|junior|jr\.?|entry|student|graduate assistant|early career)\b", re.I)),
    ("mid", re.compile(r"\bexperienced\b", re.I)),
    ("senior", re.compile(r"\b(III|IV)\b")),
    ("senior", re.compile(r"(?<![\w.-])[34]\b")),
    ("mid", re.compile(r"\bII\b(?!I)")),
    ("mid", re.compile(r"(?<![\w.-])2\b")),
    ("entry", re.compile(r"\bI\b(?![IV])")),
    ("entry", re.compile(r"(?<![\w.-])1\b")),
    ("mid", re.compile(r"\b(associate|analyst|specialist|coordinator|scientist|scholar|researcher)\b", re.I)),
    ("entry", re.compile(r"\b(assistant|aide|technician)\b", re.I)),
]


def level_of(title):
    """entry / mid / senior from the title, or "" when nothing in it says.

    A guess off the title is all that is available - no feed publishes a level
    field - so anything the rules do not recognise stays blank rather than
    being filed under a level it might not belong to.
    """
    # "Phase 3 Clinical Research Coordinator" is a trial stage, not a job grade.
    t = re.sub(r"\bphase\s*[0-9IViv]+", " ", title or "", flags=re.I)
    for name, rx in LEVEL_RULES:
        if rx.search(t):
            return name
    return ""


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

    if "apply.workable.com" in host and parts:
        return {"ats": "workable", "token": parts[0]}

    m = re.match(r"(https://[^/]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[^/]+/sites/([^/?#]+)",
                 careers_url)
    if m:
        return {"ats": "oraclecloud", "base": m.group(1), "site": m.group(2)}

    if host.endswith(".bamboohr.com"):
        return {"ats": "bamboohr", "token": host.split(".")[0]}

    if "recruiting.ultipro.com" in host:
        return {"ats": "ultipro", "url": careers_url}

    if "workforcenow.adp.com" in host:
        m = re.search(r"[?&]cid=([0-9a-fA-F\-]+)", careers_url)
        if m:
            return {"ats": "adp", "cid": m.group(1)}

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
    "clinical research", "regulatory",
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
                    # externalPath is site-relative: without the /{site} prefix
                    # the link 404s (myworkdayjobs.com/job/... treats "job" as a
                    # tenant site name that does not exist).
                    "url": f"https://{t['host']}/{t['site']}{ext}" if ext else "",
                    "posted": p.get("postedOn", ""),
                    # The list response carries no description. The same cxs
                    # path with the job appended returns one; enrich_descriptions()
                    # fetches it later, but only for postings that survive the
                    # title filter and are not already described from last sweep.
                    "detail": (f"https://{t['host']}/wday/cxs/{t['tenant']}/{t['site']}{ext}"
                               if ext else ""),
                    "detail_ats": "workday",
                })
            offset += 20
            if total and offset >= total:
                break
            time.sleep(0.6)
        time.sleep(0.3)
    return out


def pull_greenhouse(t):
    # first_published is the real posting date; updated_at moves on any edit,
    # which made months-old postings masquerade as fresh.
    # content=true returns the full posting body in the same request, so the
    # board's preview pane costs nothing extra here.
    d = fetch(f"https://boards-api.greenhouse.io/v1/boards/{t['token']}/jobs?content=true")
    return [{"title": j.get("title", ""),
             "location": (j.get("location") or {}).get("name", ""),
             "url": j.get("absolute_url", ""),
             "posted": j.get("first_published") or j.get("updated_at", ""),
             **body(j.get("content"))}
            for j in d.get("jobs", [])]


def epoch_date(ts, ms=False):
    """Epoch seconds (or milliseconds) -> 'YYYY-MM-DD', '' if absent or garbage."""
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts / 1000 if ms else ts,
                                      timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def pull_lever(t):
    d = fetch(f"https://api.lever.co/v0/postings/{t['token']}?mode=json")
    return [{"title": j.get("text", ""),
             "location": (j.get("categories") or {}).get("location", ""),
             "url": j.get("hostedUrl", ""),
             "posted": epoch_date(j.get("createdAt"), ms=True),
             **body(j.get("descriptionPlain") or j.get("description"))}
            for j in d]


def ashby_body(j):
    """Ashby is the one feed that publishes pay as a real field rather than a
    sentence, so prefer that over whatever the regex finds in the prose."""
    got = body(j.get("descriptionPlain") or j.get("descriptionHtml"))
    summary = (j.get("compensation") or {}).get("scrapeableCompensationSalarySummary") or ""
    # Only dollars: the same field carries EUR and GBP tiers, which would sort
    # into the pay filter as if the numbers were comparable. They are not.
    if summary.strip().startswith("$"):
        stated = find_pay(summary, require_label=False)
        if stated:
            got["pay"] = stated
    return got


def pull_ashby(t):
    d = fetch(f"https://api.ashbyhq.com/posting-api/job-board/{t['token']}"
              "?includeCompensation=true")
    return [{"title": j.get("title", ""),
             "location": j.get("location", ""),
             "url": j.get("jobUrl", ""),
             "posted": j.get("publishedAt", ""),
             **ashby_body(j)}
            for j in d.get("jobs", [])]


def pull_peopleadmin(t):
    """PeopleAdmin (jobs.<university>.edu) publishes an Atom feed at
    /postings/search.atom. Entries carry no location field, only a department in
    <author>, so the campus location comes from the target entry instead.
    <content> holds the posting summary, which is what the preview pane uses."""
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
            **body(e.findtext("a:content", "", ns) or e.findtext("a:summary", "", ns)),
        })
    return out


def _html(url, timeout=25):
    h = {"User-Agent": UA,
         "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
         "Accept-Language": "en-US,en;q=0.9"}
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# Same discovery list as tools/build-og.mjs, which pioneered the
# no-dependency headless-Chrome pattern in this repo.
CHROME_CANDIDATES = [
    os.environ.get("CHROME_PATH"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
]


def render_html(url, budget_ms=9000):
    """Fetch a page through headless Chrome and return the RENDERED DOM.
    Two problems urllib cannot solve, one tool: a real browser TLS fingerprint
    gets past the WAFs that 503/403 plain clients (Westat, NORC), and real JS
    execution renders the boards that ship an empty shell (Mathematica).
    GitHub Actions runners have Chrome preinstalled; set CHROME_PATH elsewhere.
    Note a WAF may still block by IP reputation, so a page that renders locally
    can fail from a datacenter runner - the carry-forward in main() covers that."""
    chrome = next((p for p in CHROME_CANDIDATES if p and Path(p).exists()), None)
    if not chrome:
        raise RuntimeError("Chrome not found; set CHROME_PATH")
    out = subprocess.run(
        [chrome, "--headless", "--disable-gpu", "--no-sandbox",
         f"--virtual-time-budget={budget_ms}", "--dump-dom", url],
        capture_output=True, timeout=120)
    dom = out.stdout.decode("utf-8", "replace") if out.stdout else ""
    if out.returncode != 0 or len(dom) < 500:
        raise RuntimeError(f"chrome render failed (rc={out.returncode}, {len(dom)} bytes)")
    return dom


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
        origin = base.split("/api/")[0]
        domain = urllib.parse.parse_qs(urllib.parse.urlparse(base).query).get("domain", [""])[0]
        for p in pos:
            locs = p.get("locations") or []
            out.append({
                "title": p.get("name", ""),
                "location": ", ".join(locs[:2]) if locs else "",
                "url": f"{ref}/job/{p.get('id')}",
                "posted": epoch_date(p.get("postedTs") or p.get("creationTs")),
                # Search results carry no body; /api/apply/v2/jobs/{id} does.
                # Fetched later, and only for the postings that survive filtering.
                "detail": f"{origin}/api/apply/v2/jobs/{p.get('id')}?domain={domain}",
                "detail_ats": "eightfold",
                "detail_ref": ref,
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
    getter = render_html if t.get("render") else _html
    seen, out = set(), []
    for page in range(t.get("pages", 1)):
        u = t["url"].replace("{page}", str(page))
        try:
            page_html = getter(u)   # NOT named "html" - that shadows the module
        except Exception:
            if page == 0:
                raise   # first page failing = source down; let main() carry prev rows
            break
        found = rx.findall(page_html)
        if not found:
            break
        for href, title in found:
            title = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", title)).strip())
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


def pull_amazonjobs(t, _retry=True):
    """Amazon's public search endpoint (amazon.jobs/en/search.json). The full
    board is tens of thousands of postings, so query per search term the same
    way the Workday puller does, and dedupe on job_path."""
    seen, out = set(), []
    for term in SEARCH_TERMS:
        offset = 0
        for _ in range(3):              # 100 per page; 300 per term is plenty
            u = ("https://www.amazon.jobs/en/search.json?result_limit=100"
                 f"&offset={offset}&base_query={urllib.parse.quote(term)}")
            try:
                d = fetch(u)
            except Exception as e:
                # transient failures here once zeroed out the whole employer,
                # so retry once with a pause the way the Workday puller does
                print(f"    ! amazon '{term}' offset {offset} failed ({e}), retrying",
                      file=sys.stderr)
                time.sleep(3)
                try:
                    d = fetch(u)
                except Exception as e2:
                    print(f"    ! gave up on '{term}': {e2}", file=sys.stderr)
                    break
            jobs = d.get("jobs") or []
            if not jobs:
                if offset == 0:   # a real zero-hit term says hits=0; anything else is throttling
                    print(f"    ! amazon '{term}': empty page, hits={d.get('hits')!r} "
                          f"error={d.get('error')!r}", file=sys.stderr)
                break
            for j in jobs:
                path = j.get("job_path", "")
                if not path or path in seen:
                    continue
                seen.add(path)
                # US only: without this, a Bengaluru posting with no US state
                # in its location string would fall back to the campus-city
                # default and masquerade as a Seattle job. Amazon uses 3-letter
                # ISO codes ("USA", "IND", "GBR").
                if j.get("country_code") and j["country_code"] not in ("US", "USA"):
                    continue
                posted = ""
                try:    # "May 28, 2026" -> ISO, so the board can parse it
                    posted = datetime.strptime(
                        j.get("posted_date", ""), "%B %d, %Y").strftime("%Y-%m-%d")
                except ValueError:
                    pass
                out.append({
                    "title": j.get("title", ""),
                    "location": j.get("normalized_location") or j.get("location", ""),
                    "url": f"https://www.amazon.jobs{path}",
                    "posted": posted,
                    **body(j.get("description"),
                           "Basic qualifications" if j.get("basic_qualifications") else "",
                           j.get("basic_qualifications")),
                })
            offset += len(jobs)
            if offset >= int(d.get("hits") or 0):
                break
            time.sleep(1.2)
        time.sleep(1.0)
    # All ten terms coming back empty is throttling, never reality - one
    # slower second pass usually gets through, and if it also comes back dry,
    # raising lets main() carry the previous sweep's rows forward.
    if not out and _retry:
        print("    ! amazon returned nothing across all terms, retrying once", file=sys.stderr)
        time.sleep(20)
        return pull_amazonjobs(t, _retry=False)
    if not out:
        raise RuntimeError("empty across all terms even after retry (throttled?)")
    return out


def pull_workable(t):
    """Workable's board API (apply.workable.com/api/v3/accounts/{token}/jobs),
    paged via the nextPage token."""
    url = f"https://apply.workable.com/api/v3/accounts/{t['token']}/jobs"
    out, page_token = [], None
    for _ in range(10):
        payload = {"query": "", "location": [], "department": [],
                   "worktype": [], "remote": []}
        if page_token:
            payload["token"] = page_token
        d = fetch(url, data=payload)
        for j in d.get("results", []):
            loc = j.get("location") or {}
            disp = ", ".join(x for x in (loc.get("city"),
                                         loc.get("region") or loc.get("country")) if x)
            if j.get("remote"):
                disp = ("Remote - " + disp) if disp else "Remote"
            out.append({
                "title": j.get("title", ""),
                "location": disp,
                "url": f"https://apply.workable.com/{t['token']}/j/{j.get('shortcode', '')}/",
                "posted": (j.get("published") or "")[:10],
            })
        page_token = d.get("nextPage")
        if not page_token:
            break
        time.sleep(0.4)
    return out


def pull_radancy(t):
    """Radancy-hosted boards (jobs.intuit.com). The search page renders in JS,
    but its ajax endpoint returns JSON whose `results` field is the rendered
    HTML, so regex the job tiles out of that. Needs "base" on the target."""
    base = t["base"]
    seen, out = set(), []
    for page in range(1, t.get("pages", 12) + 1):
        u = (f"{base}/search-jobs/results?ActiveFacetID=0&CurrentPage={page}"
             "&RecordsPerPage=50&SortCriteria=0&SortDirection=1"
             "&SearchResultsModuleName=Search+Results"
             "&SearchFiltersModuleName=Search+Filters")
        try:
            d = fetch(u, headers={"X-Requested-With": "XMLHttpRequest"})
        except Exception:
            if page == 1:
                raise   # first page failing = source down; let main() carry prev rows
            break
        tiles = d.get("results", "")
        rows = re.findall(r'<a[^>]+href="(/job/[^"]+)"[^>]*>([\s\S]{0,500}?)</a>', tiles)
        new = 0
        for href, inner in rows:
            if href in seen:
                continue
            seen.add(href)
            new += 1
            m = re.search(r"<h2[^>]*>([^<]+)</h2>", inner)
            title = (m.group(1) if m else re.sub(r"<[^>]+>", " ", inner)).strip()
            lm = re.search(r'location[^>]*>\s*([^<]{2,80})<', inner)
            out.append({
                "title": html.unescape(title),
                "location": lm.group(1).strip() if lm else t.get("location", ""),
                "url": base + href,
                "posted": "",
            })
        if not new:
            break
        time.sleep(0.5)
    return out


def pull_bamboohr(t):
    """BambooHR hosted boards: {company}.bamboohr.com/careers/list is public
    JSON. No posted date in the feed; first_seen covers recency."""
    d = fetch(f"https://{t['token']}.bamboohr.com/careers/list")
    out = []
    for j in d.get("result", []):
        loc = j.get("location") if isinstance(j.get("location"), dict) else {}
        disp = ", ".join(x for x in (loc.get("city"), loc.get("state")) if x)
        if j.get("isRemote"):
            disp = ("Remote - " + disp) if disp else "Remote"
        out.append({
            "title": j.get("jobOpeningName", ""),
            "location": disp,
            "url": f"https://{t['token']}.bamboohr.com/careers/{j.get('id')}",
            "posted": "",
        })
    return out


def pull_ultipro(t):
    """UKG Pro (UltiPro) job boards (Advarra). The board page's own search API:
    POST LoadSearchResults under the tenant + board GUID from the URL."""
    m = re.search(r"recruiting\.ultipro\.com/([A-Za-z0-9]+)/JobBoard/([a-f0-9\-]+)", t["url"], re.I)
    base = f"https://recruiting.ultipro.com/{m.group(1)}/JobBoard/{m.group(2)}"
    out, skip = [], 0
    while skip < 500:
        payload = {"opportunitySearch": {
                       "Top": 50, "Skip": skip, "QueryString": "",
                       "OrderBy": [{"Value": "postedDateDesc",
                                    "PropertyName": "PostedDate", "Ascending": False}],
                       "Filters": []},
                   "matchCriteria": {"PreferredJobs": [], "Educations": [],
                                     "LicenseAndCertifications": [], "Skills": [],
                                     "hasNoLicenses": False, "SkippedSkills": []}}
        d = fetch(f"{base}/JobBoardView/LoadSearchResults", data=payload,
                  headers={"Referer": base + "/"})
        opps = d.get("opportunities") or []
        if not opps:
            break
        for o in opps:
            locs = [l.get("LocalizedName") for l in (o.get("Locations") or [])
                    if l.get("LocalizedName")]
            out.append({
                "title": o.get("Title", ""),
                "location": "; ".join(locs[:2]),
                "url": f"{base}/OpportunityDetail?opportunityId={o.get('Id')}",
                "posted": (o.get("PostedDate") or "")[:10],
            })
        skip += len(opps)
        if skip >= (d.get("totalCount") or 0):
            break
        time.sleep(0.4)
    return out


def pull_adp(t):
    """ADP WorkforceNow career centers (Child Trends): public job-requisitions
    JSON, keyed by the cid from the career-center URL."""
    out, skip = [], 0
    while skip < 300:
        u = ("https://workforcenow.adp.com/mascsr/default/careercenter/public/events/"
             f"staffing/v1/job-requisitions?cid={t['cid']}&lang=en_US"
             f"&ccId=19000101_000001&locale=en_US&$top=50&$skip={skip}")
        d = fetch(u)
        reqs = d.get("jobRequisitions") or []
        if not reqs:
            break
        for q in reqs:
            locs = q.get("requisitionLocations") or []
            addr = (locs[0].get("address") or {}) if locs else {}
            city = ", ".join(x for x in (
                addr.get("cityName"),
                (addr.get("countrySubdivisionLevel1") or {}).get("codeValue")) if x)
            out.append({
                "title": q.get("requisitionTitle", ""),
                "location": city,
                "url": ("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/"
                        f"recruitment.html?cid={t['cid']}&ccId=19000101_000001"
                        f"&lang=en_US&jobId={q.get('itemID')}"),
                "posted": (q.get("postDate") or "")[:10],
            })
        skip += len(reqs)
        if skip >= int((d.get("meta") or {}).get("totalNumber") or 0):
            break
        time.sleep(0.4)
    return out


def pull_jibe(t):
    """Jibe / iCIMS-Attract career sites (careers.emmes.com): a clean paged
    JSON API at {base}/api/jobs, 10 per page. US-only, same reasoning as the
    Amazon puller. Needs "base" on the target."""
    base = t["base"]
    out, page, seen_count = [], 1, 0
    while page < 40:
        d = fetch(f"{base}/api/jobs?page={page}")
        jobs = d.get("jobs") or []
        if not jobs:
            break
        seen_count += len(jobs)
        for w in jobs:
            j = w.get("data", w)
            if (j.get("country_code") or "US") != "US":
                continue
            loc = (j.get("full_location") or ", ".join(
                x for x in (j.get("city"), j.get("state")) if x))
            out.append({
                "title": j.get("title", ""),
                # full_location doubles multi-site strings; first segment is enough
                "location": loc.split(";")[0].strip(),
                "url": (j.get("meta_data") or {}).get("canonical_url") or j.get("apply_url", ""),
                "posted": (j.get("posted_date") or j.get("create_date") or "")[:10],
                **body(j.get("description"), j.get("responsibilities"),
                       j.get("qualifications")),
            })
        if seen_count >= (d.get("totalCount") or 0):
            break
        page += 1
        time.sleep(0.4)
    return out


def pull_oraclecloud(t):
    """Oracle Recruiting Cloud (Abt Global). The public REST API keys on a CX_
    site number that is not in the portal URL, so scrape it off the portal page
    first. PostedDate arrives ISO. US-only, for the same reason as Amazon: the
    location strings for international postings carry no US state and would
    otherwise fall back to the campus city."""
    base, site = t["base"], t["site"]
    m = re.search(r"CX_\d+",
                  _html(f"{base}/hcmUI/CandidateExperience/en/sites/{site}/requisitions"))
    if not m:
        return []
    out, offset = [], 0
    while offset < 500:
        u = (f"{base}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
             f"?onlyData=true&expand=requisitionList.secondaryLocations"
             f"&finder=findReqs;siteNumber={m.group(0)},limit=100,offset={offset},"
             f"sortBy=POSTING_DATES_DESC")
        d = fetch(u)
        items = d.get("items") or []
        reqs = items[0].get("requisitionList", []) if items else []
        if not reqs:
            break
        for q in reqs:
            if (q.get("PrimaryLocationCountry") or "US") != "US":
                continue
            out.append({
                "title": q.get("Title", ""),
                "location": q.get("PrimaryLocation", ""),
                "url": f"{base}/hcmUI/CandidateExperience/en/sites/{site}/job/{q.get('Id')}",
                "posted": q.get("PostedDate") or "",
            })
        offset += len(reqs)
        if offset >= (items[0].get("TotalJobsCount") or 0):
            break
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
    "amazonjobs": pull_amazonjobs,
    "workable": pull_workable,
    "radancy": pull_radancy,
    "oraclecloud": pull_oraclecloud,
    "jibe": pull_jibe,
    "bamboohr": pull_bamboohr,
    "ultipro": pull_ultipro,
    "adp": pull_adp,
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
    r"philippines|ontario|quebec|toronto|"
    # global-employer feeds (Kantar, Medpace) return city-only strings with no
    # country name, which would otherwise fall through to the US campus-city
    # fallback - so the big offshore hub cities are listed explicitly
    r"manila|mandaluyong|taguig|makati|quezon city|cebu|"
    r"bengaluru|hyderabad|pune|mumbai|chennai|gurgaon|gurugram|noida|new delhi|kolkata|"
    r"warsaw|krakow|prague|budapest|bucharest|sofia|vilnius|riga|tallinn|athens|"
    r"lisbon|porto|stockholm|oslo|copenhagen|helsinki|zurich|geneva|vienna|milan|rome|"
    r"barcelona|edinburgh|glasgow|manchester, uk|leeds|belfast|"
    r"sao paulo|bogota|buenos aires|santiago|lima|montevideo|"
    r"kuala lumpur|bangkok|jakarta|hanoi|ho chi minh|taipei|seoul|hong kong|"
    r"shanghai|beijing|shenzhen|cairo|nairobi|lagos|johannesburg|dubai|tel aviv|"
    r"montreal|vancouver|ottawa|mississauga|calgary", re.I)


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


# Extracts the body out of a single-job detail response, per ATS. Only the
# feeds whose list endpoint withholds the description need an entry here.
DETAIL_DESC = {
    "workday": lambda d: (d.get("jobPostingInfo") or {}).get("jobDescription", ""),
    "eightfold": lambda d: (d.get("data") or d).get("job_description", ""),
}
MAX_DETAIL_FETCHES = 400   # one request each; bounds a runaway sweep


def enrich_bodies(rows, prev_rows, key_of):
    """Fill in the body for the ATSes that hide it behind a second request.

    Two things keep this cheap. It runs after the title filter and after
    grouping, so it only ever touches rows that made the board; and it reuses
    the previous sweep's result for rows it already knows, which means a daily
    refresh pays for the handful of genuinely new postings rather than all of
    them. A posting that fails here simply has no preview text.

    "Reuses the result" has to mean the description AND the pay range together.
    Pay is read from the full body, which is exactly what is not kept, so a
    cache hit that restored only the text would leave those rows permanently
    without pay. A previous row that predates pay extraction has no "pay" key
    at all, which is the signal to fetch it once more.
    """
    todo = [r for r in rows if not r.get("desc") and r.get("detail")]
    if not todo:
        return
    reused = fetched = 0
    for r in todo:
        cached = prev_rows.get(key_of(r))
        if cached and cached.get("desc") and "pay" in cached:
            r["desc"], r["pay"] = cached["desc"], cached["pay"]
            reused += 1
            continue
        if fetched >= MAX_DETAIL_FETCHES:
            continue
        grab = DETAIL_DESC.get(r.get("detail_ats"))
        if not grab:
            continue
        hdrs = {"Referer": r["detail_ref"]} if r.get("detail_ref") else None
        try:
            r.update(body(grab(fetch(r["detail"], headers=hdrs, timeout=20))))
            fetched += 1
        except Exception:
            pass          # no preview text for this one; the link still works
        time.sleep(0.35)  # same courtesy pause the list pagers use
    print(f"  bodies: {reused} reused from last sweep, {fetched} fetched "
          f"({len(todo) - reused - fetched} unresolved)")


def page_fingerprint(url):
    """Text-only hash of a page, so markup noise (nonces, cache-busters) does
    not read as a content change."""
    raw = _html(url)
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", raw)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip().lower()
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def build_manual(targets, prev_payload, today):
    """The check-by-hand list. Boardless employers with a watch_url also get a
    change watch: hash the page each sweep, record when it last changed, and
    the board badges recently-changed ones - "check by hand weekly" becomes
    "check when poked"."""
    prev_watch = {m.get("name"): m.get("watch") or {}
                  for m in prev_payload.get("manual", [])}
    out = []
    for t in targets:
        if t.get("careers_url") or not t.get("portal"):
            continue
        entry = {"name": t["name"], "branch": t.get("branch", ""), "portal": t["portal"]}
        if t.get("watch_url"):
            pw = prev_watch.get(t["name"], {})
            w = {"url": t["watch_url"]}
            try:
                w["hash"] = page_fingerprint(t["watch_url"])
                w["last_changed"] = (today if pw.get("hash") and w["hash"] != pw["hash"]
                                     else pw.get("last_changed", ""))
            except Exception as e:
                print(f"    ! watch fetch failed for {t['name']}: {e}", file=sys.stderr)
                w["hash"] = pw.get("hash", "")
                w["last_changed"] = pw.get("last_changed", "")
            entry["watch"] = w
        out.append(entry)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--branch", help="only sweep targets in this branch")
    ap.add_argument("--all-titles", action="store_true", help="skip the keyword filter")
    args = ap.parse_args()

    targets = json.loads((HERE / "targets.json").read_text(encoding="utf-8"))
    if args.branch:
        targets = [t for t in targets if t.get("branch") == args.branch]

    rows, skipped, failed = [], [], []
    for t in targets:
        name, careers = t["name"], t.get("careers_url", "").strip()
        if not careers:
            skipped.append((name, "no careers_url yet"))
            continue
        # A target may name its platform explicitly, for the ones whose URL shape
        # is not self-describing (eightfold, hand-written HTML rules).
        if t.get("ats"):
            info = {"ats": t["ats"], "url": careers, **{k: v for k, v in t.items()
                                                        if k in ("job_re", "base", "pages", "render")}}
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
            failed.append(name)
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
                         "level": level_of(j["title"]),
                         "posted": j["posted"], "url": j["url"],
                         "desc": j.get("desc", ""), "pay": j.get("pay"),
                         # dropped again after enrich_descriptions(); they exist
                         # only to tell it where to look for a missing body
                         "detail": j.get("detail", ""),
                         "detail_ats": j.get("detail_ats", ""),
                         "detail_ref": j.get("detail_ref", "")})
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
            # Departments post the same requisition with different bodies; take
            # the first one that actually says something rather than the first
            # one seen. Same for pay, which only some of the copies state.
            if not g.get("desc") and r.get("desc"):
                g["desc"] = r["desc"]
            if not g.get("pay") and r.get("pay"):
                g["pay"] = r["pay"]
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

    # The previous sweep's output backs three things below: stale-row
    # carry-forward, first_seen stamps, and the manual-page change watcher.
    try:
        prev_payload = json.loads((ROOT / "jobs.json").read_text(encoding="utf-8"))
    except Exception:
        prev_payload = {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Keyed by employer+title (the dedup key) rather than URL, because a grouped
    # row's representative URL changes when one of its department postings
    # closes. Used for both the description cache and the first_seen stamps.
    def seen_key(r):
        return r["employer"] + "\t" + r["title"].strip().lower()

    # Preview text and pay for the feeds that withhold the body from their list
    # endpoint. Runs here, after filtering and grouping, so it costs one request
    # per row that actually reached the board and none at all for rows already
    # known.
    enrich_bodies(rows,
                  {seen_key(j): j for j in prev_payload.get("jobs", [])},
                  seen_key)
    for r in rows:
        for k in ("detail", "detail_ats", "detail_ref"):
            r.pop(k, None)

    # A source that errors mid-sweep should not vaporize its listings until it
    # recovers (browser-tier and WAF-fronted sources especially can fail from
    # one network but not another). Carry the failed employers' previous rows
    # forward, marked stale so the data is honest about its age.
    if failed:
        failed_set = set(failed)
        carried = 0
        for pj in prev_payload.get("jobs", []):
            if pj.get("employer") in failed_set:
                pj = dict(pj)
                pj["stale"] = True
                # A row carried over from before these fields existed would
                # otherwise be missing them; the level is free to recompute,
                # the pay is not recoverable without the body.
                pj["level"] = pj.get("level") or level_of(pj.get("title", ""))
                pj.setdefault("pay", None)
                rows.append(pj)
                carried += 1
        if carried:
            print(f"  carried {carried} stale rows forward for: {', '.join(sorted(failed_set))}")

    # "First seen" survives across sweeps: carry the date forward from the
    # previous jobs.json, stamp today on rows that were not there last time.
    # Rows that predate this tracking keep "" - unknown, but old - so the board
    # never badges the whole backlog as new on the first run.
    prev_seen = {seen_key(j): j.get("first_seen", "")
                 for j in prev_payload.get("jobs", [])}
    for r in rows:
        k = seen_key(r)
        r["first_seen"] = prev_seen[k] if k in prev_seen else today

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
            "openings": sum(r.get("count", 1) for r in rows),
            "universities": sum(1 for t in targets
                                if t.get("branch") == "university" and t.get("careers_url")),
            "swept": sum(1 for t in targets if t.get("careers_url")),
            "targets": len(targets),
            "manual": build_manual(targets, prev_payload, today),
            "jobs": rows,
        }
        (ROOT / "jobs.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
        print(f"  wrote ../jobs.json ({len(rows)} jobs)")

    print(f"\n{len(rows)} hits -> results.md / results.csv")
    if skipped:
        print(f"{len(skipped)} targets skipped (see results.md)")


if __name__ == "__main__":
    main()
