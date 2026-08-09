#!/usr/bin/env python3
"""
Resolve east-coast university job feeds.

Workday site slugs are not guessable (probing common ones hit 1 of 20), but the
other platforms are:

  PeopleAdmin  fixed path /postings/search.atom, host follows jobs.<uni>.edu etc
  Greenhouse   /v1/boards/<slug>/jobs
  Lever/Ashby  same shape

So probe those patterns across the whole list, then try Workday with an expanded
slug dictionary. Whatever is left needs a browser lookup, one page each.

Writes uni_resolved.json. Safe to re-run; it is read-only against the network.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
H = {"User-Agent": UA, "Accept": "application/json,application/xml,text/html;q=0.9,*/*;q=0.8"}

# (display name, [host/slug tokens], campus location)
UNIS = [
    # --- New England ---
    ("Harvard University", ["harvard"], "Cambridge, MA"),
    ("MIT", ["mit"], "Cambridge, MA"),
    ("Boston University", ["bu"], "Boston, MA"),
    ("Boston College", ["bc"], "Chestnut Hill, MA"),
    ("Northeastern University", ["northeastern"], "Boston, MA"),
    ("Tufts University", ["tufts"], "Medford, MA"),
    ("Brandeis University", ["brandeis"], "Waltham, MA"),
    ("UMass Amherst", ["umass", "umassamherst"], "Amherst, MA"),
    ("Brown University", ["brown"], "Providence, RI"),
    ("Yale University", ["yale"], "New Haven, CT"),
    ("University of Connecticut", ["uconn"], "Storrs, CT"),
    ("Dartmouth College", ["dartmouth"], "Hanover, NH"),
    ("University of Vermont", ["uvm"], "Burlington, VT"),
    ("University of New Hampshire", ["unh"], "Durham, NH"),
    # --- New York ---
    ("Columbia University", ["columbia"], "New York, NY"),
    ("NYU", ["nyu"], "New York, NY"),
    ("Cornell University", ["cornell"], "Ithaca, NY"),
    ("University of Rochester", ["rochester"], "Rochester, NY"),
    ("Syracuse University", ["syracuse", "syr"], "Syracuse, NY"),
    ("Stony Brook University", ["stonybrook"], "Stony Brook, NY"),
    ("University at Buffalo", ["buffalo", "ub"], "Buffalo, NY"),
    ("University at Albany", ["albany"], "Albany, NY"),
    ("Fordham University", ["fordham"], "New York, NY"),
    ("Rensselaer (RPI)", ["rpi"], "Troy, NY"),
    ("CUNY", ["cuny"], "New York, NY"),
    ("Binghamton University", ["binghamton"], "Binghamton, NY"),
    # --- NJ / PA ---
    ("Princeton University", ["princeton"], "Princeton, NJ"),
    ("Rutgers University", ["rutgers"], "New Brunswick, NJ"),
    ("University of Pennsylvania", ["upenn", "penn"], "Philadelphia, PA"),
    ("Penn State", ["psu", "pennstate"], "University Park, PA"),
    ("University of Pittsburgh", ["pitt"], "Pittsburgh, PA"),
    ("Carnegie Mellon", ["cmu"], "Pittsburgh, PA"),
    ("Temple University", ["temple"], "Philadelphia, PA"),
    ("Drexel University", ["drexel"], "Philadelphia, PA"),
    ("Villanova University", ["villanova"], "Villanova, PA"),
    ("Lehigh University", ["lehigh"], "Bethlehem, PA"),
    # --- MD / DC / VA ---
    ("Johns Hopkins University", ["jhu", "johnshopkins"], "Baltimore, MD"),
    ("UMBC", ["umbc"], "Baltimore, MD"),
    ("University of Maryland Baltimore", ["umaryland", "umb"], "Baltimore, MD"),
    ("American University", ["american", "americanuniversity"], "Washington, DC"),
    ("Howard University", ["howard"], "Washington, DC"),
    ("Catholic University", ["catholic", "cua"], "Washington, DC"),
    ("Gallaudet University", ["gallaudet"], "Washington, DC"),
    ("George Mason University", ["gmu"], "Fairfax, VA"),
    ("University of Virginia", ["uva", "virginia"], "Charlottesville, VA"),
    ("Virginia Tech", ["vt", "virginiatech"], "Blacksburg, VA"),
    ("VCU", ["vcu"], "Richmond, VA"),
    ("William & Mary", ["wm", "williamandmary"], "Williamsburg, VA"),
    ("Towson University", ["towson"], "Towson, MD"),
    # --- Southeast ---
    ("Duke University", ["duke"], "Durham, NC"),
    ("UNC Chapel Hill", ["unc", "uncch"], "Chapel Hill, NC"),
    ("NC State University", ["ncsu"], "Raleigh, NC"),
    ("Wake Forest University", ["wfu", "wakeforest"], "Winston-Salem, NC"),
    ("Emory University", ["emory"], "Atlanta, GA"),
    ("Georgia Tech", ["gatech"], "Atlanta, GA"),
    ("University of Georgia", ["uga"], "Athens, GA"),
    ("University of South Carolina", ["sc", "uofsc"], "Columbia, SC"),
    ("Clemson University", ["clemson"], "Clemson, SC"),
    ("University of Florida", ["ufl"], "Gainesville, FL"),
    ("Florida State University", ["fsu"], "Tallahassee, FL"),
    ("University of Miami", ["miami"], "Coral Gables, FL"),
    ("University of South Florida", ["usf"], "Tampa, FL"),
    ("Medical University of South Carolina", ["musc"], "Charleston, SC"),
]

# Workday brute force is deliberately NOT attempted here. Slugs are arbitrary
# strings (Georgetown_Admin_Careers, UMCP), and guessing them hit 2 of ~33 across
# earlier runs while costing ~4 minutes per school. Those need a browser lookup.
PA_HOSTS = ["jobs.{t}.edu", "{t}.peopleadmin.com", "careers.{t}.edu",
            "employment.{t}.edu", "employment.{t}.com", "{t}jobs.{t}.edu",
            "apply.{t}.edu", "hr.{t}.edu"]
TIMEOUT = 6


def head_json(url, data=None, timeout=TIMEOUT):
    try:
        h = dict(H)
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            h["Content-Type"] = "application/json"
        r = urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h), timeout=timeout)
        return json.loads(r.read())
    except Exception:
        return None


def try_peopleadmin(tokens):
    for t in tokens:
        for pat in PA_HOSTS:
            host = pat.replace("{t}", t)
            url = f"https://{host}/postings/search.atom"
            try:
                b = urllib.request.urlopen(
                    urllib.request.Request(url, headers=H), timeout=TIMEOUT).read()
                n = b.count(b"<entry")
                if n:
                    return "peopleadmin", url, n
            except Exception:
                pass
    return None


def try_ats(tokens):
    for t in tokens:
        d = head_json(f"https://boards-api.greenhouse.io/v1/boards/{t}/jobs")
        if d and d.get("jobs"):
            return "greenhouse", f"https://job-boards.greenhouse.io/{t}", len(d["jobs"])
        d = head_json(f"https://api.lever.co/v0/postings/{t}?mode=json")
        if isinstance(d, list) and d:
            return "lever", f"https://jobs.lever.co/{t}", len(d)
        d = head_json(f"https://api.ashbyhq.com/posting-api/job-board/{t}")
        if d and d.get("jobs"):
            return "ashby", f"https://jobs.ashbyhq.com/{t}", len(d["jobs"])
    return None


def probe(entry):
    name, tokens, loc = entry
    hit = try_peopleadmin(tokens) or try_ats(tokens)
    return name, tokens, loc, hit


def main():
    from concurrent.futures import ThreadPoolExecutor

    out, misses = {}, []
    with ThreadPoolExecutor(max_workers=12) as pool:
        for i, (name, tokens, loc, hit) in enumerate(pool.map(probe, UNIS), 1):
            if hit:
                kind, url, n = hit
                out[name] = {"ats": kind, "careers_url": url, "location": loc, "count": n}
                print(f"[{i}/{len(UNIS)}] HIT  {name}: {kind} {url} ({n})", flush=True)
            else:
                misses.append(name)
                print(f"[{i}/{len(UNIS)}] --   {name}", flush=True)

    (HERE / "uni_resolved.json").write_text(
        json.dumps({"resolved": out, "misses": misses}, indent=2), encoding="utf-8")
    print(f"\nresolved {len(out)} of {len(UNIS)}; {len(misses)} need a browser lookup")


if __name__ == "__main__":
    main()
