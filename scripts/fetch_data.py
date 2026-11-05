#!/usr/bin/env python3
"""Download the raw inputs for the Senate cost-per-vote build into data/raw/.

Resumable: files and API pages that already exist are skipped, and when the FEC
API rate limit is hit the script waits and retries. DEMO_KEY allows very few
calls per window (in practice 10, then an ~18-hour lockout), so set FEC_API_KEY,
free from https://api.data.gov/signup/, to get every outside-spending figure exact.
"""
import csv
import json
import os
import pathlib
import time
import urllib.error
import urllib.request
import zipfile

RAW = pathlib.Path(__file__).resolve().parent.parent / "data" / "raw"
API = "https://api.open.fec.gov/v1"
KEY = os.environ.get("FEC_API_KEY", "DEMO_KEY")
UA = {"User-Agent": "vote-cost-build/1.0"}
FILES = {
    "1976-2024-senate-state.csv": "https://dataverse.harvard.edu/api/access/datafile/13887039?format=original",
    "weball24.zip": "https://www.fec.gov/files/bulk-downloads/2024/weball24.zip",
    "cn24.zip": "https://www.fec.gov/files/bulk-downloads/2024/cn24.zip",
    "webk24.zip": "https://www.fec.gov/files/bulk-downloads/2024/webk24.zip",
    "independent_expenditure_2024.csv": "https://www.fec.gov/files/bulk-downloads/2024/independent_expenditure_2024.csv",
}
RETRY_WAIT = 900  # seconds to wait after an HTTP 429


def get(url):
    while True:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as resp:
                return resp.read()
        except urllib.error.HTTPError as err:
            if err.code != 429:
                raise
            print(f"rate limited; waiting {RETRY_WAIT}s", flush=True)
            time.sleep(RETRY_WAIT)


def fetch_pages(prefix, path, stop_when_zero=False):
    """Save every page of a paginated FEC endpoint as <prefix>_p<n>.json."""
    page, pages = 1, 1
    while page <= pages:
        out = RAW / f"{prefix}_p{page}.json"
        if out.exists():
            body = out.read_bytes()
        else:
            body = get(f"{API}{path}&api_key={KEY}&per_page=100&page={page}")
            json.loads(body)  # never cache a truncated response
            out.write_bytes(body)
            print(f"saved {out.name}", flush=True)
        parsed = json.loads(body)
        pages = parsed["pagination"]["pages"]
        page += 1
        if stop_when_zero and parsed["results"] and not parsed["results"][-1]["disbursements"]:
            break  # sorted by spending, so every later page is all zeros


def senate_ie_candidates():
    """Senate candidates with any independent expenditure, from the bulk line items.

    The line items contain duplicates and amendments, so they are only trusted for
    *who* had outside spending. FEC's aggregates supply the amounts, and filtering
    that endpoint to these IDs drops House and presidential rows, saving many API calls.
    """
    with open(RAW / "independent_expenditure_2024.csv", encoding="latin-1", newline="") as handle:
        return sorted({r["cand_id"] for r in csv.DictReader(handle) if r["can_office"] == "S" and r["cand_id"]})


def main():
    RAW.mkdir(parents=True, exist_ok=True)
    for name, url in FILES.items():
        if not (RAW / name).exists():
            (RAW / name).write_bytes(get(url))
            print(f"saved {name}", flush=True)
    for name in ("weball24", "cn24", "webk24"):
        with zipfile.ZipFile(RAW / f"{name}.zip") as archive:
            archive.extractall(RAW)
    fetch_pages("fec_totals", "/candidates/totals/?office=S&election_year=2024&cycle=2024&election_full=true&sort=-disbursements", stop_when_zero=True)
    ids = "".join(f"&candidate_id={cid}" for cid in senate_ie_candidates())
    fetch_pages("fec_ie", f"/schedules/schedule_e/totals/by_candidate/?cycle=2024&election_full=true&sort=-total{ids}")


if __name__ == "__main__":
    main()
