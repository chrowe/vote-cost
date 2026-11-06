# Senate cost per vote, 2024

What every 2024 U.S. Senate race cost per general-election vote.

**Live site:** https://chrowe.github.io/vote-cost/

Open `index.html` in a browser. There is nothing to install or serve, and the
filters recalculate in the page. Results are also in
`data/senate_2024_cost_per_vote.csv`.

## The calculation

    cost per vote = (campaign spending + outside spending) / general-election votes

The page explains each input and lets you change what counts. Defaults: all
candidates who filed for the seat, money spent in 2023-24, and outside spending
for or against them.

## Rebuilding the data

Needs Python 3.9+ and nothing else.

    export FEC_API_KEY=your_key   # free: https://api.data.gov/signup/
    python3 scripts/fetch_data.py
    python3 scripts/build_data.py

`fetch_data.py` skips anything already in `data/raw/`. `build_data.py` writes
`data/races.js` and the CSV, and stops with an error if the join looks wrong.

The bulk downloads are not in git, so a fresh clone needs `fetch_data.py` before
it can rebuild. The FEC API responses behind the shipped data are committed. FEC
can revise its bulk files, so a later download may move the numbers slightly.

**Make the outside spending exact.** The shipped data was built without a personal key,
so FEC's own totals cover about 95% of outside money and the rest is summed from
filing records. With a key, the commands above fetch every total and the page's
methodology note updates itself. Without one, `DEMO_KEY` locks out after about
10 calls for roughly 18 hours.

## Testing

    python3 tests/test_page.py

Needs Python 3.9+ and Google Chrome, and nothing else. No downloads are needed, so
it runs from a fresh clone. If Chrome isn't found, set `CHROME` to its path. The tests
recalculate every displayed figure from `data/races.js` under every filter combination,
and use real mouse movement for the tooltip. They also break figures in a copy of the
page to confirm the checks notice. They run on every pull request through GitHub Actions.

## Data decisions worth knowing

| Issue | Handling |
|---|---|
| Undervotes and void ballots in MIT's data | Removed, so only votes for someone count |
| Fusion candidates on several ballot lines | Summed into one person |
| California's regular and special elections | One row: same finalists, same money |
| One person with several FEC IDs | Merged by state and name; rows are copies, so the largest is kept |
| Filers typing stale, House, blank or wrong candidate IDs | Bulk rows matched by state and surname |
| Lucas Kunce's committee converted to a PAC after the election | Spending taken from the PAC summary file |
| Wesley Bell and Matt Rosendale | Excluded: their one committee mostly funded House campaigns |

## Sources

- FEC: [API](https://api.open.fec.gov/developers/) and [bulk data](https://www.fec.gov/data/browse-data/?tab=bulk-data)
- [MIT Election Data + Science Lab, U.S. Senate statewide 1976-2024](https://doi.org/10.7910/DVN/PEJ5QU) (CC0), checked against the [House Clerk's 2024 statistics](https://clerk.house.gov/member_info/electionInfo/2024/statistics2024.pdf)

## License

The code is under the [MIT License](LICENSE). That doesn't change the terms of the
source data: FEC data and the MIT Election Lab's CC0 dataset keep their own terms.
