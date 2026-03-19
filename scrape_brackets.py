"""
ESPN Tournament Challenge - Bracket Picks Scraper
Reusable year over year.

Requirements:
    pip install requests pandas

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONFIGURATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Edit config.json with your group's values before running.
Command line args override config.json if both are provided.

    config.json:
    {
        "group_id":     "your-group-uuid-here",
        "challenge_id": 277
    }

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
USAGE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Normal run (reads from config.json):
    py scrape_brackets.py

Override config values from the command line:
    py scrape_brackets.py --group GROUP_ID --challenge 285

Test with first 3 brackets only:
    py scrape_brackets.py --test

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW TO FIND YOUR IDs EACH YEAR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Group ID:
    It's in your group URL:
    fantasy.espn.com/games/tournament-challenge-bracket-YEAR/group?id=GROUP_ID

Challenge ID:
    Open your group page, F12 -> Network -> Fetch/XHR -> reload.
    Look for: gambit-api.fantasy.espn.com/apis/v1/challenges/CHALLENGE_ID/groups/...
    The number after /challenges/ is the challenge ID.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Saved to data/brackets_YEAR.csv. One row per bracket, columns:
  entry_name, owner_name, entry_id, score, rank,
  R64_G01 ... R64_G32,   <- Round of 64 (32 games)
  R32_G01 ... R32_G16,   <- Round of 32 (16 games)
  S16_G01 ... S16_G08,   <- Sweet 16    ( 8 games)
  E8_G01  ... E8_G04,    <- Elite 8     ( 4 games)
  FF_G01  ... FF_G02,    <- Final Four  ( 2 games)
  Champ_G01              <- Championship( 1 game )

Each cell contains the picked team, e.g. "(1) Duke" or "(3) Michigan St"
"""

import requests
import pandas as pd
import json
import time
import argparse
import sys
from pathlib import Path
from collections import Counter

BASE = "https://gambit-api.fantasy.espn.com/apis/v1"

PERIOD_NAMES = {1: "R64", 2: "R32", 3: "S16", 4: "E8", 5: "FF", 6: "Champ"}

HEADERS = {
    "User-Agent":  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/146.0 Safari/537.36",
    "Referer":     "https://fantasy.espn.com/",
    "Origin":      "https://fantasy.espn.com",
    "Accept":      "application/json, text/plain, */*",
    "x-fantasy-platform": "chui",
    "x-fantasy-source":   "chui",
}

SESSION = requests.Session()
CONFIG_FILE = Path(__file__).parent / "config.json"


# ── Config loading ────────────────────────────────────────────────────────────

def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def resolve(cli_val, config_val, default):
    """CLI arg > config file > default."""
    if cli_val is not None:
        return cli_val
    if config_val is not None:
        return config_val
    return default


# ── HTTP ──────────────────────────────────────────────────────────────────────

def get_json(url, params=None):
    r = SESSION.get(url, headers=HEADERS, params=params, timeout=20)
    print(f"    {r.status_code}  {url}")
    if r.status_code != 200:
        print(f"    Response: {r.text[:300]}")
    r.raise_for_status()
    return r.json()


# ── Year detection ───────────────────────────────────────────────────────────

def fetch_year(challenge_id):
    """Fetch the tournament year from the challenge metadata."""
    data = get_json(f"{BASE}/challenges/{challenge_id}/",
                    {"platform": "chui", "view": "chui_default"})
    key = data.get("key", "")   # e.g. "tournament-challenge-bracket-2026"
    for part in key.split("-"):
        if part.isdigit() and len(part) == 4:
            return int(part)
    raise ValueError(f"Could not parse year from challenge key: '{key}'")


# ── Step 1: Propositions (matchup + team lookup) ──────────────────────────────

def fetch_propositions(challenge_id):
    """
    Returns:
      outcome_lookup : { outcomeId -> {team, seed, abbrev, col, round} }
      prop_lookup    : { propositionId -> col_name }
      col_order      : ordered list of column names (R64_G01 ... Champ_G01)
    """
    data  = get_json(f"{BASE}/propositions/",
                     {"challengeId": challenge_id, "platform": "chui", "view": "chui_default"})
    props = data if isinstance(data, list) else data.get("propositions", [])
    print(f"    {len(props)} propositions fetched")

    props.sort(key=lambda p: (p.get("scoringPeriodId", 0), p.get("displayOrder", 0)))

    outcome_lookup = {}
    prop_lookup    = {}
    col_order      = []
    round_counters = {}

    for prop in props:
        prop_id     = prop["id"]
        period      = prop.get("scoringPeriodId", 0)
        round_label = PERIOD_NAMES.get(period, f"R{period}")

        round_counters[round_label] = round_counters.get(round_label, 0) + 1
        col_name = f"{round_label}_G{round_counters[round_label]:02d}"
        col_order.append(col_name)
        prop_lookup[prop_id] = col_name

        for outcome in prop.get("possibleOutcomes", []):
            oid    = outcome["id"]
            team   = outcome.get("name", "")
            abbrev = outcome.get("abbrev", "")
            seed   = outcome.get("regionSeed", "")

            if not seed:
                for m in outcome.get("mappings", []):
                    if m.get("type") == "SEED":
                        seed = m.get("value", "")
                        break

            outcome_lookup[oid] = {
                "team": team, "abbrev": abbrev, "seed": seed,
                "col": col_name, "round": round_label,
            }

    round_counts = Counter(v["round"] for v in outcome_lookup.values())
    for r in PERIOD_NAMES.values():
        n = round_counts.get(r, 0)
        if n:
            print(f"    {r}: {n // 2} games")

    return outcome_lookup, prop_lookup, col_order


# ── Step 2: Group entries ─────────────────────────────────────────────────────

def fetch_group_entries(challenge_id, group_id, page_size=50):
    entries = []
    offset  = 0
    while True:
        filter_obj = json.dumps({"filterSortId": {"value": 0},
                                 "limit": page_size, "offset": offset})
        data = get_json(
            f"{BASE}/challenges/{challenge_id}/groups/{group_id}/",
            {"platform": "chui", "view": "chui_default_group", "filter": filter_obj}
        )
        page = data.get("entries", [])
        if not page:
            break
        for e in page:
            name      = e.get("name", "")
            score_obj = e.get("score") or {}
            owner     = ((e.get("entryMetadata") or {}).get("ownerDisplayName")
                         or e.get("member", {}).get("displayName", ""))
            entries.append({
                "entry_id":   e["id"],
                "entry_name": name,
                "owner_name": owner,
                "score":      score_obj.get("overallScore", ""),
                "rank":       score_obj.get("rank", ""),
            })
        print(f"    {len(page)} entries fetched (total: {len(entries)})")
        if len(page) < page_size:
            break
        offset += page_size
        time.sleep(0.2)
    return entries


# ── Step 3: One entry's picks ─────────────────────────────────────────────────

def fetch_entry_picks(challenge_id, entry_id, outcome_lookup):
    data   = get_json(f"{BASE}/challenges/{challenge_id}/entries/{entry_id}/",
                      {"platform": "chui", "view": "chui_default"})
    result = {}
    for pick in data.get("picks", []):
        outcomes = pick.get("outcomesPicked", [])
        if not outcomes:
            continue
        oid = outcomes[0].get("outcomeId", "")
        if oid in outcome_lookup:
            info = outcome_lookup[oid]
            seed = info["seed"]
            team = info["team"]
            result[info["col"]] = f"({seed}) {team}" if seed else team
    return result


# ── Step 4: Build DataFrame ───────────────────────────────────────────────────

def build_dataframe(entries, challenge_id, outcome_lookup, col_order):
    rows  = []
    total = len(entries)

    for i, entry in enumerate(entries, 1):
        eid   = entry["entry_id"]
        ename = entry["entry_name"]
        oname = entry["owner_name"]
        print(f"  [{i:>2}/{total}] {ename}  ({oname or '?'})")

        try:
            picks = fetch_entry_picks(challenge_id, eid, outcome_lookup)
        except Exception as exc:
            print(f"         FAILED: {exc}")
            picks = {}

        row = {
            "entry_name": ename,
            "owner_name": oname,
            "entry_id":   eid,
            "score":      entry.get("score", ""),
            "rank":       entry.get("rank", ""),
        }
        for col in col_order:
            row[col] = picks.get(col, "")
        rows.append(row)
        time.sleep(0.25)

    meta = ["entry_name", "owner_name", "entry_id", "score", "rank"]
    return pd.DataFrame(rows, columns=meta + col_order)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="ESPN Tournament Challenge bracket scraper — reusable year over year"
    )
    parser.add_argument("--group",     default=None, help="Group ID (overrides config.json)")
    parser.add_argument("--challenge", default=None, help="Challenge ID (overrides config.json)")
    parser.add_argument("--output",    default=None, help="Output CSV path (default: data/brackets_YEAR.csv)")
    parser.add_argument("--test",      action="store_true", help="Only process first 3 brackets")
    args = parser.parse_args()

    config = load_config()

    group_id     = resolve(args.group,     config.get("group_id"),     None)
    challenge_id = resolve(args.challenge, config.get("challenge_id"), None)

    if not group_id:
        print("Error: group_id not set. Add it to config.json or pass --group.")
        sys.exit(1)
    if not challenge_id:
        print("Error: challenge_id not set. Add it to config.json or pass --challenge.")
        sys.exit(1)

    challenge_id = int(challenge_id)

    if args.test:
        print("*** TEST MODE: first 3 brackets ***\n")

    print("=" * 60)
    print(f"ESPN Tournament Challenge | Challenge ID {challenge_id}")
    print(f"Group: {group_id}")
    print("=" * 60)

    print("\n> Detecting tournament year...")
    year = fetch_year(challenge_id)
    print(f"    Year: {year}")

    # Default output goes into a data/ folder next to this script
    if args.output:
        out_path = Path(args.output)
    else:
        data_dir = Path(__file__).parent / "data"
        data_dir.mkdir(exist_ok=True)
        out_path = data_dir / f"brackets_{year}.csv"

    print("\n> Step 1: Fetching propositions (teams + matchups)...")
    outcome_lookup, prop_lookup, col_order = fetch_propositions(challenge_id)
    if not outcome_lookup:
        print("No outcomes found. Check challenge_id in config.json.")
        sys.exit(1)

    print("\n> Step 2: Fetching group entries...")
    entries = fetch_group_entries(challenge_id, group_id)
    print(f"    {len(entries)} total")

    if not entries:
        print("No entries found. Check group_id in config.json.")
        sys.exit(1)

    if args.test:
        entries = entries[:3]

    print(f"\n> Step 3: Fetching picks ({len(entries)} brackets)...")
    df = build_dataframe(entries, challenge_id, outcome_lookup, col_order)

    df.to_csv(out_path, index=False)

    game_cols      = [c for c in df.columns if any(c.startswith(r + "_") for r in PERIOD_NAMES.values())]
    total_picks    = sum(1 for col in game_cols for val in df[col] if val)
    missing_owners = df["owner_name"].isna().sum() + (df["owner_name"] == "").sum()

    print(f"\nDone!")
    print(f"  {len(df)} brackets x {len(game_cols)} game columns")
    print(f"  {total_picks} / {len(df) * len(game_cols)} picks captured")
    if missing_owners:
        missing = list(df.loc[df["owner_name"].isna() | (df["owner_name"] == ""), "entry_name"])
        print(f"  {missing_owners} entries missing owner name: {missing}")
    print(f"  Saved -> {out_path.resolve()}")

    if args.test and len(df):
        print(f"\nSample — {df.iloc[0]['entry_name']} ({df.iloc[0]['owner_name']}):")
        for col in game_cols[:12]:
            v = df.iloc[0][col]
            if v:
                print(f"  {col}: {v}")


if __name__ == "__main__":
    main()
