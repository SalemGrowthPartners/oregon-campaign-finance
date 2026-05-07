#!/usr/bin/env python3
"""
Oregon campaign finance collector.
Fetches transactions filed in a date window from ORESTAR and writes to SQLite.

Usage:
    python collector.py                        # last 7 days (default)
    python collector.py --days 30              # last N days
    python collector.py --from 04/01/2026 --to 04/30/2026
    python collector.py --db path/to/file.db   # custom DB path
"""

import argparse
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
    import xlrd
    import yaml
except ImportError:
    print("Missing dependencies. Run: pip install requests beautifulsoup4 xlrd pyyaml")
    sys.exit(1)

BASE_URL = "https://secure.sos.state.or.us/orestar"
SEARCH_URL = f"{BASE_URL}/gotoPublicTransactionSearch.do"
DEFAULT_DB = Path(__file__).parent / "campaign_finance.db"
DEFAULT_WATCHLIST = Path(__file__).parent / "watchlist.yaml"

# Maps XLS Sub Type strings to canonical txn_type values
def _txn_type(sub_type: str) -> str:
    s = sub_type.lower()
    if "contribution" in s:
        return "contribution"
    if "expenditure" in s:
        return "expenditure"
    return "other"


def _iso_date(val):
    """Convert MM/DD/YYYY string (or xlrd date float) to YYYY-MM-DD."""
    if not val:
        return None
    if isinstance(val, float):
        # xlrd returns dates as floats when format isn't detected
        d = xlrd.xldate_as_datetime(val, 0)
        return d.strftime("%Y-%m-%d")
    s = str(val).strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return s  # already ISO or unknown format


# ---------------------------------------------------------------------------
# ORESTAR fetch
# ---------------------------------------------------------------------------

def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; research scraper; oregoncf-pipeline)",
    })
    return s


def fetch_xls(from_date: str, to_date: str, verbose: bool = True) -> bytes:
    """
    Runs the 3-step ORESTAR protocol and returns raw XLS bytes.
    from_date / to_date: MM/DD/YYYY strings (filed date range).
    """
    s = _build_session()

    if verbose:
        print(f"[1/3] GET search form …")
    resp = s.get(SEARCH_URL, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    form = soup.find("form", {"method": lambda m: m and m.lower() == "post"})
    if not form:
        raise RuntimeError("Could not find POST form on ORESTAR search page")

    action = form.get("action", "")
    if action.startswith("/"):
        post_url = "https://secure.sos.state.or.us" + action
    else:
        post_url = f"{BASE_URL}/{action}"

    if verbose:
        print(f"[2/3] Fetch CSRF token …")
    csrf_resp = s.post(
        f"{BASE_URL}/JavaScriptServlet",
        headers={"FETCH-CSRF-TOKEN": "1"},
        timeout=15,
    )
    csrf_resp.raise_for_status()
    parts = csrf_resp.text.strip().split(":", 1)
    if len(parts) != 2:
        raise RuntimeError(f"Unexpected CSRF response: {csrf_resp.text!r}")
    token_name, token_value = parts[0].strip(), parts[1].strip()

    payload = {}
    for inp in form.find_all("input", type="hidden"):
        payload[inp.get("name", "")] = inp.get("value", "")
    payload.update({
        "cneSearchTranFiledStartDate": from_date,
        "cneSearchTranFiledEndDate":   to_date,
        "cneSearchButtonName":         "search",
        token_name:                    token_value,
    })

    if verbose:
        print(f"[3/3] POST search ({from_date} → {to_date}) …")
    search_resp = s.post(
        post_url,
        data=payload,
        headers={"Referer": SEARCH_URL},
        timeout=30,
    )
    search_resp.raise_for_status()

    if "csrfInvalid=true" in search_resp.url:
        raise RuntimeError("CSRF validation failed — session may have expired mid-request")

    # Brief pause before the export request
    time.sleep(1)

    if verbose:
        print(f"     Downloading XLS export …")
    xls_resp = s.get(
        f"{BASE_URL}/XcelCNESearch",
        headers={"Referer": search_resp.url},
        timeout=120,
    )
    xls_resp.raise_for_status()

    content_type = xls_resp.headers.get("Content-Type", "")
    if "excel" not in content_type and "spreadsheet" not in content_type:
        raise RuntimeError(
            f"Expected XLS response, got Content-Type={content_type!r}. "
            "Search may have returned no results or an error page."
        )

    if verbose:
        print(f"     {len(xls_resp.content):,} bytes received")
    return xls_resp.content


# ---------------------------------------------------------------------------
# XLS parsing
# ---------------------------------------------------------------------------

# Column indices in the ORESTAR export (confirmed 2026-05-05)
_COL = {
    "txn_id":          0,
    "tran_date":       2,
    "committee_name":  4,
    "contributor":     5,
    "txn_subtype":     6,
    "amount":          8,
    "committee_id":   11,
    "purpose":        19,
    "filed_date":     24,
    "contributor_type": 26,
    "occupation":     28,
    "employer":       29,
    "city":           36,
    "state":          37,
}


def parse_xls(xls_bytes: bytes) -> list[dict]:
    wb = xlrd.open_workbook(file_contents=xls_bytes)
    ws = wb.sheet_by_index(0)

    records = []
    for row_idx in range(1, ws.nrows):  # skip header row
        row = ws.row(row_idx)

        def cell(col):
            v = row[col].value
            return str(v).strip() if v != "" else ""

        txn_id = cell(_COL["txn_id"])
        if not txn_id:
            continue  # skip blank/footer rows

        sub_type = cell(_COL["txn_subtype"])
        amount_raw = row[_COL["amount"]].value
        try:
            amount = float(amount_raw) if amount_raw != "" else None
        except (ValueError, TypeError):
            amount = None

        records.append({
            "txn_id":          txn_id,
            "committee_id":    cell(_COL["committee_id"]),
            "committee_name":  cell(_COL["committee_name"]),
            "txn_type":        _txn_type(sub_type),
            "txn_subtype":     sub_type,
            "purpose":         cell(_COL["purpose"]),
            "amount":          amount,
            "txn_date":        _iso_date(cell(_COL["tran_date"])),
            "filed_date":      _iso_date(cell(_COL["filed_date"])),
            "contributor":     cell(_COL["contributor"]),
            "contributor_type": cell(_COL["contributor_type"]),
            "employer":        cell(_COL["employer"]),
            "occupation":      cell(_COL["occupation"]),
            "city":            cell(_COL["city"]),
            "state":           cell(_COL["state"]),
        })

    return records


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

def load_watchlist(path: Path) -> dict:
    """
    Returns:
        {
          "races":      {committee_id: {"name": str, "candidate": str, ...}},
          "thresholds": {key: value},
        }
    """
    if not path.exists():
        return {"races": {}, "thresholds": {}}

    with open(path) as f:
        raw = yaml.safe_load(f)

    races = {}
    for entry in raw.get("races", []):
        cid = str(entry["id"])
        races[cid] = {
            "name":         entry.get("name", ""),
            "candidate":    entry.get("candidate", ""),
            "cycle":        entry.get("cycle"),
            "jurisdiction": entry.get("jurisdiction", ""),
        }

    return {
        "races":      races,
        "thresholds": raw.get("thresholds", {}),
    }


def race_label(race_entry: dict) -> str:
    """Human-readable label stored in the race column."""
    candidate = race_entry.get("candidate", "")
    name = race_entry.get("name", "")
    if candidate and name:
        return f"{name} — {candidate}"
    return candidate or name


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    txn_id           TEXT PRIMARY KEY,
    committee_id     TEXT,
    committee_name   TEXT,
    txn_type         TEXT,
    txn_subtype      TEXT,
    purpose          TEXT,
    amount           REAL,
    txn_date         DATE,
    filed_date       DATE,
    contributor      TEXT,
    contributor_type TEXT,
    employer         TEXT,
    occupation       TEXT,
    city             TEXT,
    state            TEXT,
    race             TEXT,
    first_seen       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_txn_filed_date    ON transactions(filed_date);
CREATE INDEX IF NOT EXISTS idx_txn_committee_id  ON transactions(committee_id);
CREATE INDEX IF NOT EXISTS idx_txn_type          ON transactions(txn_type);
CREATE INDEX IF NOT EXISTS idx_txn_amount        ON transactions(amount);
"""

INSERT_SQL = """
INSERT OR IGNORE INTO transactions
    (txn_id, committee_id, committee_name, txn_type, txn_subtype, purpose,
     amount, txn_date, filed_date, contributor, contributor_type,
     employer, occupation, city, state, race)
VALUES
    (:txn_id, :committee_id, :committee_name, :txn_type, :txn_subtype, :purpose,
     :amount, :txn_date, :filed_date, :contributor, :contributor_type,
     :employer, :occupation, :city, :state, :race)
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def write_records(
    conn: sqlite3.Connection,
    records: list[dict],
    race_lookup: dict,
) -> tuple[int, int]:
    """
    Insert records into the DB, deduplicating on txn_id.
    race_lookup: {committee_id: race_label_string} from load_watchlist().
    Returns (inserted, skipped) counts.
    """
    # Apply race tags to incoming records
    for rec in records:
        rec["race"] = race_lookup.get(rec["committee_id"])

    before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    conn.executemany(INSERT_SQL, records)
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    inserted = after - before
    skipped = len(records) - inserted

    # Backfill race tags on any previously-inserted records that lacked them
    if race_lookup:
        for cid, label in race_lookup.items():
            conn.execute(
                "UPDATE transactions SET race = ? WHERE committee_id = ? AND race IS NULL",
                (label, cid),
            )
        conn.commit()

    return inserted, skipped


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Collect Oregon campaign finance data from ORESTAR")
    p.add_argument("--days",  type=int, default=7,
                   help="Number of days back to pull (default: 7)")
    p.add_argument("--from",  dest="from_date", metavar="MM/DD/YYYY",
                   help="Filed-date range start (overrides --days)")
    p.add_argument("--to",    dest="to_date",   metavar="MM/DD/YYYY",
                   help="Filed-date range end (default: today)")
    p.add_argument("--db",    default=str(DEFAULT_DB), metavar="PATH",
                   help=f"SQLite database path (default: {DEFAULT_DB})")
    p.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST), metavar="PATH",
                   help=f"Watchlist YAML path (default: {DEFAULT_WATCHLIST})")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch and parse but do not write to database")
    return p.parse_args()


def main():
    args = parse_args()

    today = date.today()
    to_date   = args.to_date   or today.strftime("%m/%d/%Y")
    from_date = args.from_date or (today - timedelta(days=args.days)).strftime("%m/%d/%Y")

    watchlist = load_watchlist(Path(args.watchlist))
    race_lookup = {
        cid: race_label(entry)
        for cid, entry in watchlist["races"].items()
    }

    print(f"Filed date range: {from_date} → {to_date}")
    print(f"Database:         {args.db}")
    print(f"Watchlist:        {len(race_lookup)} committees tagged")
    if args.dry_run:
        print("(dry run — will not write to database)")
    print()

    xls_bytes = fetch_xls(from_date, to_date)
    records = parse_xls(xls_bytes)
    print(f"Parsed {len(records):,} records from XLS")

    if args.dry_run:
        tagged = [r for r in records if race_lookup.get(r["committee_id"])]
        print(f"Tagged {len(tagged):,} records match watchlist committees")
        if tagged:
            print(f"\nSample watchlist record:")
            for k, v in {**tagged[0], "race": race_lookup.get(tagged[0]["committee_id"])}.items():
                print(f"  {k}: {v!r}")
        elif records:
            print(f"\nSample record (no watchlist match):")
            for k, v in records[0].items():
                print(f"  {k}: {v!r}")
        return

    db_path = Path(args.db)
    conn = init_db(db_path)
    inserted, skipped = write_records(conn, records, race_lookup)
    conn.close()

    print(f"Wrote to {db_path}")
    print(f"  Inserted: {inserted:,} new records")
    print(f"  Skipped:  {skipped:,} already in DB (deduped on txn_id)")


if __name__ == "__main__":
    main()
