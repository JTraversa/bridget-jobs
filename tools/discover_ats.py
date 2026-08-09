#!/usr/bin/env python3
"""
Discover each university's ATS by crawling its HR/careers landing pages.

Guessing Workday slugs does not work (2 hits in ~33 attempts). But universities
almost always LINK to their ATS from the HR page, so fetch a handful of likely
landing pages per school and regex the HTML for known ATS URL shapes.

Writes ats_discovered.json. Read-only against the network.
"""

import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
H = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 9

# name -> (domain, campus location)
SCHOOLS = {
    "Harvard University": ("harvard.edu", "Cambridge, MA"),
    "MIT": ("mit.edu", "Cambridge, MA"),
    "Boston University": ("bu.edu", "Boston, MA"),
    "Boston College": ("bc.edu", "Chestnut Hill, MA"),
    "Northeastern University": ("northeastern.edu", "Boston, MA"),
    "Tufts University": ("tufts.edu", "Medford, MA"),
    "Brandeis University": ("brandeis.edu", "Waltham, MA"),
    "UMass Amherst": ("umass.edu", "Amherst, MA"),
    "Brown University": ("brown.edu", "Providence, RI"),
    "Yale University": ("yale.edu", "New Haven, CT"),
    "University of Connecticut": ("uconn.edu", "Storrs, CT"),
    "University of Vermont": ("uvm.edu", "Burlington, VT"),
    "University of New Hampshire": ("unh.edu", "Durham, NH"),
    "Columbia University": ("columbia.edu", "New York, NY"),
    "NYU": ("nyu.edu", "New York, NY"),
    "Cornell University": ("cornell.edu", "Ithaca, NY"),
    "University of Rochester": ("rochester.edu", "Rochester, NY"),
    "Syracuse University": ("syracuse.edu", "Syracuse, NY"),
    "Stony Brook University": ("stonybrook.edu", "Stony Brook, NY"),
    "University at Buffalo": ("buffalo.edu", "Buffalo, NY"),
    "University at Albany": ("albany.edu", "Albany, NY"),
    "Rensselaer (RPI)": ("rpi.edu", "Troy, NY"),
    "CUNY": ("cuny.edu", "New York, NY"),
    "Binghamton University": ("binghamton.edu", "Binghamton, NY"),
    "Princeton University": ("princeton.edu", "Princeton, NJ"),
    "Rutgers University": ("rutgers.edu", "New Brunswick, NJ"),
    "University of Pennsylvania": ("upenn.edu", "Philadelphia, PA"),
    "Penn State": ("psu.edu", "University Park, PA"),
    "University of Pittsburgh": ("pitt.edu", "Pittsburgh, PA"),
    "Carnegie Mellon": ("cmu.edu", "Pittsburgh, PA"),
    "Temple University": ("temple.edu", "Philadelphia, PA"),
    "Drexel University": ("drexel.edu", "Philadelphia, PA"),
    "Lehigh University": ("lehigh.edu", "Bethlehem, PA"),
    "Johns Hopkins University": ("jhu.edu", "Baltimore, MD"),
    "UMBC": ("umbc.edu", "Baltimore, MD"),
    "University of Maryland Baltimore": ("umaryland.edu", "Baltimore, MD"),
    "American University": ("american.edu", "Washington, DC"),
    "Howard University": ("howard.edu", "Washington, DC"),
    "Catholic University": ("catholic.edu", "Washington, DC"),
    "Gallaudet University": ("gallaudet.edu", "Washington, DC"),
    "George Mason University": ("gmu.edu", "Fairfax, VA"),
    "University of Virginia": ("virginia.edu", "Charlottesville, VA"),
    "Virginia Tech": ("vt.edu", "Blacksburg, VA"),
    "VCU": ("vcu.edu", "Richmond, VA"),
    "William & Mary": ("wm.edu", "Williamsburg, VA"),
    "Towson University": ("towson.edu", "Towson, MD"),
    "Duke University": ("duke.edu", "Durham, NC"),
    "Wake Forest University": ("wfu.edu", "Winston-Salem, NC"),
    "Emory University": ("emory.edu", "Atlanta, GA"),
    "Georgia Tech": ("gatech.edu", "Atlanta, GA"),
    "University of Georgia": ("uga.edu", "Athens, GA"),
    "Clemson University": ("clemson.edu", "Clemson, SC"),
    "University of Florida": ("ufl.edu", "Gainesville, FL"),
    "Florida State University": ("fsu.edu", "Tallahassee, FL"),
    "University of Miami": ("miami.edu", "Coral Gables, FL"),
    "University of South Florida": ("usf.edu", "Tampa, FL"),
    "MUSC": ("musc.edu", "Charleston, SC"),
}

LANDINGS = ["https://hr.{d}/careers", "https://hr.{d}", "https://careers.{d}",
            "https://{d}/careers", "https://jobs.{d}", "https://{d}/jobs",
            "https://humanresources.{d}", "https://hr.{d}/jobs",
            "https://{d}/human-resources", "https://employment.{d}",
            # vanity hosts that proxy an ATS
            "https://hiring.{d}", "https://careers.{d}/viewalljobs",
            "https://careers.{d}/jobs", "https://apply.{d}",
            "https://hr.{d}/careers/careers-home", "https://working.{d}"]

PATTERNS = [
    # Some tenants serve from wd{N}.myworkdaysite.com/recruiting/{tenant}/{site}
    # instead of {tenant}.wd{N}.myworkdayjobs.com. Same CXS API underneath.
    ("workday", re.compile(
        r"https?://(wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/"
        r"([a-z0-9\-]+)/([A-Za-z0-9_\-]{2,60})", re.I)),
    ("workday", re.compile(
        r"https?://([a-z0-9\-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:wday/[a-z]+/[^/]+/)?"
        r"(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_\-]{2,60})", re.I)),
    ("peopleadmin", re.compile(r"https?://([a-z0-9\.\-]+)/postings/search", re.I)),
    ("icims", re.compile(r"https?://([a-z0-9\.\-]+\.icims\.com)", re.I)),
    ("taleo", re.compile(r"https?://([a-z0-9\.\-]+\.taleo\.net)", re.I)),
    ("csod", re.compile(r"https?://([a-z0-9\.\-]+\.csod\.com)", re.I)),
    ("greenhouse", re.compile(r"https?://(?:boards|job-boards)\.greenhouse\.io/([a-z0-9_\-]+)", re.I)),
    ("pageup", re.compile(r"https?://([a-z0-9\.\-]+\.pageuppeople\.com)", re.I)),
    ("interviewexchange", re.compile(r"https?://([a-z0-9\.\-]*interviewexchange\.com)", re.I)),
]

SKIP_SLUGS = {"wday", "cxs", "en-US", "job", "jobs", "login", "assets", "static"}


def get(url):
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=TIMEOUT)
        return r.read(500_000).decode("utf-8", "replace")
    except Exception:
        return ""


def discover(item):
    name, (domain, loc) = item
    seen = {}
    for pat in LANDINGS:
        html = get(pat.format(d=domain))
        if not html:
            continue
        for kind, rx in PATTERNS:
            m = rx.search(html)
            if not m:
                continue
            if kind == "workday":
                if seen.get("workday"):
                    continue
                a, b, c = m.group(1), m.group(2), m.group(3)
                if a.lower().startswith("wd"):       # myworkdaysite: wd{N}/tenant/site
                    wd, tenant, slug = a, b, c
                else:                                 # myworkdayjobs: tenant/wd{N}/site
                    tenant, wd, slug = a, b, c
                if slug in SKIP_SLUGS:
                    continue
                seen[kind] = f"https://{tenant}.{wd}.myworkdayjobs.com/{slug}"
            elif kind == "peopleadmin":
                seen[kind] = f"https://{m.group(1)}/postings/search.atom"
            elif kind == "greenhouse":
                seen[kind] = f"https://job-boards.greenhouse.io/{m.group(1)}"
            else:
                seen[kind] = m.group(0)
        # workday/peopleadmin/greenhouse are the ones the sweep can actually read
        if seen.get("workday") or seen.get("peopleadmin") or seen.get("greenhouse"):
            break
    return name, domain, loc, seen


def main():
    out, misses = {}, []
    with ThreadPoolExecutor(max_workers=12) as pool:
        for name, domain, loc, seen in pool.map(discover, SCHOOLS.items()):
            usable = seen.get("workday") or seen.get("peopleadmin") or seen.get("greenhouse")
            if usable:
                kind = ("workday" if seen.get("workday") else
                        "peopleadmin" if seen.get("peopleadmin") else "greenhouse")
                out[name] = {"ats": kind, "careers_url": usable, "location": loc,
                             "other": {k: v for k, v in seen.items() if k != kind}}
                print(f"HIT  {name}: {kind}  {usable}", flush=True)
            else:
                misses.append({"name": name, "domain": domain, "found": seen})
                print(f"--   {name}: {seen or 'nothing'}", flush=True)

    (HERE / "ats_discovered.json").write_text(
        json.dumps({"resolved": out, "misses": misses}, indent=2), encoding="utf-8")
    print(f"\nresolved {len(out)} of {len(SCHOOLS)}")


if __name__ == "__main__":
    main()
