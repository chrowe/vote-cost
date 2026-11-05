#!/usr/bin/env python3
"""Join FEC money and MIT Election Lab votes into data/races.js and a CSV.

Run scripts/fetch_data.py first. Uses only the standard library.
The page does the cost-per-vote arithmetic, so its filters stay live; this script
only produces clean per-candidate rows and fails loudly if the join looks wrong.
"""
import collections
import csv
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"

# Ballot rows that are not votes for anyone. "OTHER" (write-ins) and Nevada's
# "NONE OF THESE CANDIDATES" are real choices, so they stay in the denominator.
NON_VOTES = {"UNDERVOTES", "OVERVOTES", "VOID", "BLANK", "BLANKS"}
NON_CANDIDATES = NON_VOTES | {"OTHER", "NONE OF THESE CANDIDATES"}

# Their Senate bids were brief and their one committee kept running a House campaign,
# so the FEC's Senate-ID row is a copy of House spending, not money spent on this race.
HOUSE_CAMPAIGNS = {"S4MO00250", "S8MT00234"}  # Wesley Bell, Matt Rosendale

# Nebraska held two unrelated Senate elections. Everyone else filed for the regular seat.
NE_SPECIAL_IDS = {"S6NE00129", "S4NE00223"}

# Ballot name -> FEC ID where the last name alone is ambiguous or differs.
BALLOT_TO_FEC = {("MO", "W. C. YOUNG"): None, ("MO", "JARED YOUNG"): "S4MO00284"}

WEBALL_COLUMNS = (
    "id name ici pty party receipts trans_from disb trans_to coh_bop coh_cop cand_contrib "
    "cand_loans other_loans cand_repay other_repay debts indiv state district special primary "
    "runoff general general_pct other_cmte party_cmte cvg_end indiv_refunds cmte_refunds"
).split()
PARTY_LABEL = {"DEM": "D", "DFL": "D", "REP": "R", "IND": "I", "LIB": "L", "GRE": "G"}
TITLES = re.compile(r"(?<![A-Z])(SEN|MR|MRS|MS|DR|HON|REV|PROF|NO|LT|COL|RET|MAJ|GEN|CAPT|SGT)(?![A-Z])\.?")
SUFFIX = re.compile(r"(?<![A-Z])(JR|SR|II|III|IV)(?![A-Z])\.?")
MONEY_NOTES = {
    "MO": ("Lucas Kunce's campaign committee was later converted to a PAC (Heartland Patriots), so the FEC's "
           "candidate summary omits it. His campaign figure comes from that committee's own 2023-24 total."),
}


def pages(prefix):
    """Results from saved API pages, plus whether every page is present."""
    files = sorted(RAW.glob(f"{prefix}_p*.json"), key=lambda p: int(p.stem.rsplit("p", 1)[1]))
    bodies = [json.loads(path.read_text()) for path in files]
    if not bodies:
        return [], False
    return [row for body in bodies for row in body["results"]], len(bodies) == bodies[0]["pagination"]["pages"]


def title_case(raw):
    def fix(word):
        quote = word[0] if word[0] in "\"'" else ""
        word = quote + word[len(quote):].capitalize()
        if word == "Larose":
            return "LaRose"
        if word.startswith("Mc") and len(word) > 2:
            word = "Mc" + word[2:].capitalize()
        if word.startswith("O'") and len(word) > 2:
            word = "O'" + word[2:].capitalize()
        return "-".join(p[:1 + len(quote)].upper() + p[1 + len(quote):] if i == 0 else p[:1].upper() + p[1:]
                        for i, p in enumerate(word.split("-")))
    words = [fix(w) for w in raw.split()]
    keep = {"Jr.": "Jr.", "Jr": "Jr.", "Ii": "II", "Iii": "III"}
    return " ".join(keep.get(w, w) for w in words)


def ballot_name(raw):
    name = re.sub(r"^DR\s+", "", raw.upper())
    return title_case(name)


def fec_name(raw):
    last, _, first = raw.partition(",")
    first = TITLES.sub("", first.upper())
    suffix = SUFFIX.search(first)
    first = SUFFIX.sub("", first)
    first = re.sub(r"[#.,]+$", "", re.sub(r"\s+", " ", first)).strip()
    return title_case(" ".join(part for part in (first, last, suffix and suffix.group(0)) if part))


def last_name(fec):
    return re.sub(r"[^A-Z -]", "", fec.partition(",")[0].upper()).strip()


def load_votes():
    contests = collections.defaultdict(lambda: collections.defaultdict(int))
    with open(RAW / "1976-2024-senate-state.csv", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["year"] != "2024":
                continue
            if row["candidate"].upper() in NON_VOTES:
                continue
            key = (row["state_po"], row["state"].title(), row["special"] == "True")
            # Fusion candidates appear once per ballot line (CT's Murphy); sum them.
            contests[key][(row["candidate"], row["party_simplified"])] += int(float(row["candidatevotes"]))
    merged = {}
    for key, rows in contests.items():
        by_name = collections.defaultdict(lambda: {"votes": 0, "line_votes": 0, "party": "OTHER"})
        for (name, party), votes in rows.items():
            entry = by_name[name]
            entry["votes"] += votes
            if votes > entry["line_votes"]:  # label a fusion candidate by their main ballot line
                entry["line_votes"], entry["party"] = votes, party
        merged[key] = by_name
    return merged


def load_two_year():
    """2023-24 disbursements by candidate ID, and by committee for the PAC fallback."""
    candidates, committees = {}, {}
    for line in (RAW / "weball24.txt").read_text(encoding="latin-1").splitlines():
        row = dict(zip(WEBALL_COLUMNS, line.split("|")))
        if row["id"].startswith("S"):
            candidates[row["id"]] = float(row["disb"] or 0)
    for line in (RAW / "webk24.txt").read_text(encoding="latin-1").splitlines():
        fields = line.split("|")
        committees[fields[0]] = float(fields[12] or 0)
    return candidates, committees


def surname(name):
    name = name.upper()
    raw = name.partition(",")[0] if "," in name else (name.split() or [""])[-1]
    return re.sub(r"[^A-Z -]", "", raw).strip()


def bulk_outside(people):
    """Sum the bulk line items per candidate. Returns totals and the money left unassigned.

    Filers often type a stale, House or blank candidate ID, and sometimes another
    candidate's, so state + surname decides who a row belongs to and the ID only
    breaks ties. Filings replaced by a later amendment are dropped.
    """
    by_name = collections.defaultdict(set)
    canonical = {}
    for person in people:
        by_name[(person["state"], surname(person["fec"]))].add(person["id"])
        canonical.update((alias, person["id"]) for alias in [person["id"]] + person["aliases"])
    with open(RAW / "independent_expenditure_2024.csv", encoding="latin-1", newline="") as handle:
        rows = [r for r in csv.DictReader(handle) if r["can_office"] == "S" and r["sup_opp"] in ("S", "O")]
    replaced = {(r["spe_id"], r["prev_file_num"]) for r in rows if r["prev_file_num"]}
    totals, unassigned = collections.Counter(), collections.Counter()
    for row in rows:
        if (row["spe_id"], row["file_num"]) in replaced:
            continue
        amount = float(row["exp_amo"] or 0)
        cid = canonical.get(row["cand_id"])
        matches = by_name.get((row["can_office_state"], surname(row["cand_name"])), set())
        if len(matches) == 1:
            totals[(next(iter(matches)), row["sup_opp"])] += amount
        elif cid:  # ambiguous surname or a mistyped state
            totals[(cid, row["sup_opp"])] += amount
        else:
            unassigned[(row["can_office_state"], surname(row["cand_name"]), row["sup_opp"])] += amount
    return totals, unassigned


def load_outside(people):
    """Return per-candidate outside money, leftover rows, and whether FEC totals cover it all.

    FEC's own aggregates already fold alternate IDs into the candidate, so they are
    used wherever we have them. A person with two IDs needs both before the
    aggregate can be trusted; otherwise that side comes from the bulk data.
    """
    full, complete = pages("fec_ie")
    top = full if complete else json.loads((RAW / "fec_ie_top100.json").read_text())["results"]
    exact = {(r["candidate_id"], r["support_oppose_indicator"]): r["total"] for r in top}
    estimate, unassigned = bulk_outside(people)
    outside = collections.defaultdict(lambda: {"S": 0.0, "O": 0.0})
    for person in people:
        cid = person["id"]
        for side in ("S", "O"):
            members = [(m, side) for m in [cid] + person["aliases"]]
            if complete or all(m in exact for m in members):
                outside[cid][side] = sum(exact.get(m, 0.0) for m in members)
            else:
                outside[cid][side] = estimate[(cid, side)]
    return outside, unassigned, complete


def seat_key(state, fec_id):
    if state == "NE" and fec_id in NE_SPECIAL_IDS:
        return ("NE", True)
    return (state, False)  # CA's special shares candidates and money, so it folds into one row


def load_roster(up_states):
    """Everyone who filed for a 2024 seat, per the FEC candidate master file.

    The full-cycle API figure is only a source of six-year spending: it reports $0
    for some real candidates, so it cannot define who ran or what they spent.
    """
    two_year, committees = load_two_year()
    six_year = {row["candidate_id"]: row["disbursements"] or 0.0 for row in pages("fec_totals")[0]}
    filings = collections.defaultdict(list)
    for line in (RAW / "cn.txt").read_text(encoding="latin-1").splitlines():
        cn = line.split("|")
        cid, name, party, year, state, office, status, committee = cn[0], cn[1], cn[2], cn[3], cn[4], cn[5], cn[7], cn[9]
        if office != "S" or year != "2024" or state not in up_states or cid in HOUSE_CAMPAIGNS:
            continue
        # A committee converted after the election leaves the candidate summary but stays in the PAC file.
        spend2 = two_year[cid] if cid in two_year else committees.get(committee, 0.0)
        first = (TITLES.sub("", name.partition(",")[2].upper()).split() or [""])[0]
        filings[(state, surname(name), first)].append({
            "id": cid,
            "fec": name,
            "state": state,
            "name": fec_name(name),
            "party": PARTY_LABEL.get(party, "O"),
            "status": status,
            "spend2": spend2,
            # Six years can't be less than the last two; the API's $0s would otherwise win.
            "spend6": max(six_year.get(cid, 0.0), spend2),
        })
    people = []
    for rows in filings.values():
        # One person can hold several IDs. Their rows repeat the same committee, so keep
        # the largest rather than adding them up.
        rows.sort(key=lambda r: (-r["spend2"], -r["spend6"], r["id"]))
        keep = rows[0]
        keep.update(aliases=[r["id"] for r in rows[1:]], spend2=round(keep["spend2"], 2),
                    spend6=round(keep["spend6"], 2), votes=None, pct=None)
        keep["for"] = keep["against"] = 0.0
        people.append(keep)
    return people


def build():
    votes = load_votes()
    up_states = {key[0] for key in votes}
    people_all = load_roster(up_states)
    outside, unassigned, outside_exact = load_outside(people_all)
    roster = collections.defaultdict(list)
    for person in people_all:
        person["for"] = round(outside[person["id"]]["S"], 2)
        person["against"] = round(outside[person["id"]]["O"], 2)
        roster[seat_key(person["state"], person["id"])].append(person)

    races, problems = [], []
    grouped = collections.defaultdict(dict)
    for (st, state, special), rows in votes.items():
        grouped[(st, state, special if st == "NE" else False)][special] = rows

    for (st, state, special), contests in sorted(grouped.items()):
        primary = contests[False] if st == "CA" else contests[special]
        total = sum(e["votes"] for e in primary.values())
        people = roster[(st, special)]
        ballot = []
        for name, entry in primary.items():
            if name.upper() in NON_CANDIDATES:
                continue
            if (st, name) in BALLOT_TO_FEC:
                fid = BALLOT_TO_FEC[(st, name)]
                match = [c for c in people if c["id"] == fid]
            else:
                clean = re.sub(r"[^A-Z -]", "", name.upper())
                match = [c for c in people if re.search(rf"\b{re.escape(last_name(c['fec']))}\b", clean)]
            if len(match) > 1:
                problems.append(f"{st}: {name} matches {[c['fec'] for c in match]}")
                match = []
            pct = entry["votes"] / total
            if match:
                match[0].update(votes=entry["votes"], pct=round(pct, 5), name=ballot_name(name))
            else:
                if pct >= 0.05:
                    problems.append(f"{st}: {name} has {pct:.1%} but no FEC record")
                elif pct >= 0.01:
                    print(f"note: {st} {name} ({pct:.1%}) has no FEC spending record")
                # Minor candidates who raised nothing can still draw outside money.
                key = surname(name)
                ballot.append({
                    "id": None, "name": ballot_name(name), "fec": "", "state": st, "aliases": [],
                    "party": {"DEMOCRAT": "D", "REPUBLICAN": "R", "LIBERTARIAN": "L"}.get(entry["party"], "O"),
                    "status": "", "spend2": 0.0, "spend6": 0.0,
                    "for": round(unassigned.pop((st, key, "S"), 0.0), 2),
                    "against": round(unassigned.pop((st, key, "O"), 0.0), 2),
                    "votes": entry["votes"], "pct": round(pct, 5),
                })
        # Dozens of filers raised nothing and never made the ballot; they would only add noise.
        everyone = [
            {k: v for k, v in c.items() if k not in ("fec", "state", "aliases")}
            for c in people + ballot
            if c["votes"] or c["spend2"] or c["spend6"] or c["for"] or c["against"]
        ]
        finishers = sorted((c for c in everyone if c["votes"]), key=lambda c: -c["votes"])
        winner, second = finishers[0], finishers[1]
        note = ""
        if st == "CA":
            spec = sum(e["votes"] for e in contests[True].values())
            note = (f"California voted twice on the same ballot, for the full term and for the rest of "
                    f"Feinstein's term, with the same two finalists and one pot of money. This row uses "
                    f"the full-term vote; the special drew {spec:,} more votes.")
        if st == "NE":
            note = "Nebraska held two separate Senate elections with different candidates, listed as separate rows."
        note = note or MONEY_NOTES.get(st, "")
        races.append({
            "id": f"{st}{'-special' if special else ''}",
            "state": state,
            "st": st,
            "special": special,
            "votes": total,
            "winner": winner["name"],
            "winnerParty": winner["party"],
            "margin": round((winner["votes"] - second["votes"]) / total, 5),
            "note": note,
            "candidates": sorted(everyone, key=lambda c: (-(c["votes"] or 0), -c["spend2"])),
        })

    check(races, problems, unassigned, up_states)
    return races, outside_exact


def check(races, problems, unassigned, up_states):
    leftovers = sorted(((v, k) for k, v in unassigned.items() if k[0] in up_states), reverse=True)
    print(f"Outside money in these states matching no 2024 candidate (excluded): ${sum(v for v, _ in leftovers):,.0f}")
    for amount, key in leftovers[:10]:
        print(f"  {key} ${amount:,.0f}")
    if len(races) != 34:
        problems.append(f"expected 34 rows (35 contests, CA folded), got {len(races)}")
    for race in races:
        if race["candidates"][0]["votes"] / race["votes"] < 0.3:
            problems.append(f"{race['id']}: implausible winner share")
        for person in race["candidates"][:2]:
            if person["spend2"] < 10000:  # how a missing committee shows up, as with Kunce
                problems.append(f"{race['id']}: finalist {person['name']} shows ${person['spend2']:,.0f}")
    if problems:
        raise SystemExit("Join problems:\n  " + "\n  ".join(problems))


def write(races, outside_exact):
    payload = json.dumps({"outsideExact": outside_exact, "races": races}, separators=(",", ":"), ensure_ascii=False)
    (ROOT / "data" / "races.js").write_text(f"window.SENATE_2024 = {payload};\n", encoding="utf-8")
    with open(ROOT / "data" / "senate_2024_cost_per_vote.csv", "w", newline="", encoding="utf-8") as handle:
        out = csv.writer(handle)
        out.writerow(["race", "winner", "party", "votes", "candidate_spending_2023_24", "outside_spending",
                      "total", "cost_per_vote", "general_candidates_only_cost_per_vote"])
        for race in sorted(races, key=lambda r: -cost(r, False)):
            people = race["candidates"]
            campaign = sum(c["spend2"] for c in people)
            outside = sum(c["for"] + c["against"] for c in people)
            out.writerow([race["state"] + (" (special)" if race["special"] else ""), race["winner"],
                          race["winnerParty"], race["votes"], round(campaign), round(outside),
                          round(campaign + outside), round(cost(race, False), 2), round(cost(race, True), 2)])


def cost(race, general_only):
    people = [c for c in race["candidates"] if c["votes"] or not general_only]
    return sum(c["spend2"] + c["for"] + c["against"] for c in people) / race["votes"]


def main():
    races, outside_exact = build()
    write(races, outside_exact)
    print("outside spending:", "FEC aggregates" if outside_exact else "FEC aggregates + bulk estimates")
    money = sum(cost(r, False) * r["votes"] for r in races)
    votes = sum(r["votes"] for r in races)
    print(f"{len(races)} rows, ${money:,.0f}, {votes:,} votes, ${money / votes:.2f} per vote")
    for race in sorted(races, key=lambda r: -cost(r, False)):
        print(f"{race['id']:>11} ${cost(race, False):7.2f}  ${cost(race, True):7.2f}  {race['votes']:>10,}  {race['winner']}")


if __name__ == "__main__":
    main()
