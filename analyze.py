#!/usr/bin/env python3
"""
Pre-analysis aggregator for Oregon campaign finance data.
Queries the SQLite DB and produces a structured text summary
ready to be injected into the Claude API digest prompt.

Usage:
    python analyze.py                        # last 7 days (default)
    python analyze.py --days 30
    python analyze.py --from 04/01/2026 --to 04/30/2026
    python analyze.py --output summary.txt   # write to file instead of stdout
"""

import argparse
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Missing dependency: pip install pyyaml")
    sys.exit(1)

DEFAULT_DB        = Path(__file__).parent / "campaign_finance.db"
DEFAULT_WATCHLIST = Path(__file__).parent / "watchlist.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_thresholds(watchlist_path: Path) -> dict:
    if not watchlist_path.exists():
        return {"large_donation": 5000, "large_expenditure": 10000,
                "employer_cluster_count": 3, "late_filing_days": 25}
    with open(watchlist_path) as f:
        raw = yaml.safe_load(f)
    return raw.get("thresholds", {})


def load_tier_lookup(watchlist_path: Path) -> dict:
    """Returns {race_label: tier} for all watchlist entries that have a tier."""
    if not watchlist_path.exists():
        return {}
    with open(watchlist_path) as f:
        raw = yaml.safe_load(f)
    result = {}
    for entry in raw.get("races", []):
        candidate = entry.get("candidate", "")
        name      = entry.get("name", "")
        label     = f"{name} — {candidate}" if candidate and name else (candidate or name)
        tier      = entry.get("tier", "")
        if label and tier:
            result[label] = tier
    return result


def fmt_dollars(n) -> str:
    if n is None:
        return "$0"
    return f"${n:,.0f}"


def fmt_date(s) -> str:
    if not s:
        return "unknown"
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%b %-d, %Y")
    except ValueError:
        return s


def days_between(d1: str, d2: str):
    try:
        a = datetime.strptime(d1, "%Y-%m-%d")
        b = datetime.strptime(d2, "%Y-%m-%d")
        return (b - a).days
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Query functions — each returns a list of dicts
# ---------------------------------------------------------------------------

def q(conn, sql, params=()):
    conn.row_factory = sqlite3.Row
    cur = conn.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def race_summary(conn, from_date, to_date):
    return q(conn, """
        SELECT
            race,
            committee_name,
            committee_id,
            SUM(CASE WHEN txn_type='contribution' THEN amount ELSE 0 END) AS contributions,
            COUNT(CASE WHEN txn_type='contribution' THEN 1 END)            AS contribution_count,
            SUM(CASE WHEN txn_type='expenditure'  THEN amount ELSE 0 END) AS expenditures,
            COUNT(CASE WHEN txn_type='expenditure' THEN 1 END)             AS expenditure_count
        FROM transactions
        WHERE filed_date >= ? AND filed_date <= ?
          AND race IS NOT NULL
        GROUP BY committee_id
        ORDER BY contributions DESC
    """, (from_date, to_date))


def top_contributions(conn, from_date, to_date, limit=25):
    return q(conn, """
        SELECT committee_name, race, contributor, contributor_type,
               amount, txn_date, filed_date, city, state, employer, occupation
        FROM transactions
        WHERE filed_date >= ? AND filed_date <= ?
          AND txn_type = 'contribution'
          AND race IS NOT NULL
        ORDER BY amount DESC
        LIMIT ?
    """, (from_date, to_date, limit))


def top_expenditures(conn, from_date, to_date, limit=20):
    return q(conn, """
        SELECT committee_name, race, contributor AS vendor,
               amount, txn_date, purpose, txn_subtype
        FROM transactions
        WHERE filed_date >= ? AND filed_date <= ?
          AND txn_type = 'expenditure'
          AND race IS NOT NULL
        ORDER BY amount DESC
        LIMIT ?
    """, (from_date, to_date, limit))


def flag_late_filers(conn, from_date, to_date, threshold_days):
    rows = q(conn, """
        SELECT committee_name, race, contributor, amount, txn_type,
               txn_date, filed_date,
               CAST(JULIANDAY(filed_date) - JULIANDAY(txn_date) AS INTEGER) AS lag_days
        FROM transactions
        WHERE filed_date >= ? AND filed_date <= ?
          AND JULIANDAY(filed_date) - JULIANDAY(txn_date) >= ?
        ORDER BY lag_days DESC
        LIMIT 20
    """, (from_date, to_date, threshold_days))
    return rows


def flag_employer_clusters(conn, from_date, to_date, min_count):
    return q(conn, """
        SELECT employer, committee_name, race,
               COUNT(*) AS donor_count,
               SUM(amount) AS total,
               GROUP_CONCAT(DISTINCT contributor) AS donors
        FROM transactions
        WHERE filed_date >= ? AND filed_date <= ?
          AND txn_type = 'contribution'
          AND employer != '' AND employer IS NOT NULL
        GROUP BY LOWER(employer), committee_id
        HAVING donor_count >= ?
        ORDER BY donor_count DESC, total DESC
        LIMIT 15
    """, (from_date, to_date, min_count))


def flag_vendor_concentration(conn, from_date, to_date):
    """Flag committees where a single vendor captured >= 60% of expenditures."""
    committee_totals = q(conn, """
        SELECT committee_id, SUM(amount) AS total_spend
        FROM transactions
        WHERE filed_date >= ? AND filed_date <= ?
          AND txn_type = 'expenditure'
          AND race IS NOT NULL
        GROUP BY committee_id
        HAVING total_spend > 0
    """, (from_date, to_date))

    flags = []
    for ct in committee_totals:
        vendors = q(conn, """
            SELECT committee_name, race, contributor AS vendor,
                   COUNT(*) AS n, SUM(amount) AS vendor_total
            FROM transactions
            WHERE filed_date >= ? AND filed_date <= ?
              AND txn_type = 'expenditure'
              AND committee_id = ?
            GROUP BY contributor
            ORDER BY vendor_total DESC
            LIMIT 1
        """, (from_date, to_date, ct["committee_id"]))
        if vendors:
            v = vendors[0]
            pct = (v["vendor_total"] or 0) / ct["total_spend"] * 100
            if pct >= 60 and v["n"] >= 2:
                flags.append({**v, "pct": pct, "committee_total_spend": ct["total_spend"]})
    return sorted(flags, key=lambda x: x["vendor_total"], reverse=True)


def flag_out_of_state(conn, from_date, to_date, min_amount=1000):
    return q(conn, """
        SELECT committee_name, race, contributor, contributor_type,
               amount, city, state, employer
        FROM transactions
        WHERE filed_date >= ? AND filed_date <= ?
          AND txn_type = 'contribution'
          AND state NOT IN ('OR', '') AND state IS NOT NULL
          AND amount >= ?
        ORDER BY amount DESC
        LIMIT 20
    """, (from_date, to_date, min_amount))


def flag_entity_anomalies(conn, from_date, to_date, min_amount):
    return q(conn, """
        SELECT committee_name, race, contributor, contributor_type,
               amount, city, state
        FROM transactions
        WHERE filed_date >= ? AND filed_date <= ?
          AND txn_type = 'contribution'
          AND contributor_type NOT IN ('Individual', '')
          AND contributor_type IS NOT NULL
          AND amount >= ?
        ORDER BY amount DESC
        LIMIT 20
    """, (from_date, to_date, min_amount))


def flag_large_donations(conn, from_date, to_date, threshold):
    """Any single contribution above threshold — regardless of watchlist."""
    return q(conn, """
        SELECT committee_name, race, contributor, contributor_type,
               amount, txn_date, filed_date, city, state
        FROM transactions
        WHERE filed_date >= ? AND filed_date <= ?
          AND txn_type = 'contribution'
          AND amount >= ?
        ORDER BY amount DESC
        LIMIT 20
    """, (from_date, to_date, threshold))


def notable_non_watchlist_committees(conn, from_date, to_date, min_total):
    """Committees not in the watchlist with significant activity this period."""
    return q(conn, """
        SELECT committee_id, committee_name,
               SUM(CASE WHEN txn_type='contribution' THEN amount ELSE 0 END) AS contributions,
               SUM(CASE WHEN txn_type='expenditure'  THEN amount ELSE 0 END) AS expenditures,
               COUNT(*) AS n
        FROM transactions
        WHERE filed_date >= ? AND filed_date <= ?
          AND race IS NULL
        GROUP BY committee_id
        HAVING contributions >= ?
        ORDER BY contributions DESC
        LIMIT 15
    """, (from_date, to_date, min_total))


def period_totals(conn, from_date, to_date):
    row = q(conn, """
        SELECT
            COUNT(*) AS total_txns,
            COUNT(DISTINCT committee_id) AS committees,
            SUM(CASE WHEN txn_type='contribution' THEN amount ELSE 0 END) AS total_contributions,
            SUM(CASE WHEN txn_type='expenditure'  THEN amount ELSE 0 END) AS total_expenditures,
            COUNT(CASE WHEN race IS NOT NULL THEN 1 END)                   AS watchlist_txns
        FROM transactions
        WHERE filed_date >= ? AND filed_date <= ?
    """, (from_date, to_date))
    return row[0] if row else {}


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def build_report(conn, from_date: str, to_date: str, thresholds: dict, tier_lookup: dict = None) -> str:
    large_donation    = thresholds.get("large_donation", 5000)
    large_expenditure = thresholds.get("large_expenditure", 10000)
    cluster_count     = thresholds.get("employer_cluster_count", 3)
    late_days         = thresholds.get("late_filing_days", 25)
    tier_lookup       = tier_lookup or {}
    _TIER_ORDER       = ["statewide", "legislative", "local"]

    lines = []
    def out(s=""):
        lines.append(s)

    # Header
    out("=" * 70)
    out("OREGON CAMPAIGN FINANCE — PRE-ANALYSIS SUMMARY")
    out(f"Period:    {fmt_date(from_date)} – {fmt_date(to_date)} (transaction filed date)")
    out(f"Generated: {date.today().strftime('%B %-d, %Y')}")
    out("=" * 70)

    # Period totals
    totals = period_totals(conn, from_date, to_date)
    out()
    out("PERIOD TOTALS (all Oregon committees)")
    out(f"  Transactions filed:   {totals.get('total_txns', 0):,}")
    out(f"  Unique committees:    {totals.get('committees', 0):,}")
    out(f"  Total contributions:  {fmt_dollars(totals.get('total_contributions'))}")
    out(f"  Total expenditures:   {fmt_dollars(totals.get('total_expenditures'))}")
    out(f"  Watchlist transactions:{totals.get('watchlist_txns', 0):,}")

    # Race summary table — grouped by tier when tier_lookup is available
    races = race_summary(conn, from_date, to_date)
    if races:
        out()
        out("─" * 70)
        out("WATCHLIST RACES — PERIOD SUMMARY")
        out("─" * 70)

        def _emit_race(r):
            out()
            out(f"  {r['race']}")
            out(f"  Committee: {r['committee_name']}  (ID: {r['committee_id']})")
            out(f"  Contributions: {fmt_dollars(r['contributions'])}  "
                f"({r['contribution_count']} transactions)")
            out(f"  Expenditures:  {fmt_dollars(r['expenditures'])}  "
                f"({r['expenditure_count']} transactions)")

        if tier_lookup:
            by_tier = {t: [] for t in _TIER_ORDER}
            untiered = []
            for r in races:
                t = tier_lookup.get(r["race"], "")
                (by_tier[t] if t in by_tier else untiered).append(r)
            for t in _TIER_ORDER:
                if by_tier[t]:
                    out()
                    out(f"  [{t.upper()}]")
                    for r in by_tier[t]:
                        _emit_race(r)
            for r in untiered:
                _emit_race(r)
        else:
            for r in races:
                _emit_race(r)

    # Top contributions by race
    contribs = top_contributions(conn, from_date, to_date)
    if contribs:
        out()
        out("─" * 70)
        out("TOP CONTRIBUTIONS (watchlist races, by amount)")
        out("─" * 70)
        for c in contribs:
            lag = days_between(c["txn_date"], c["filed_date"])
            lag_str = f"  [filed {lag}d after txn]" if lag and lag >= 20 else ""
            employer_str = f", employer: {c['employer']}" if c.get("employer") else ""
            location = f"{c['city']}, {c['state']}" if c.get("city") and c.get("state") else (c.get("state") or "")
            out()
            out(f"  {fmt_dollars(c['amount']):>12}  {c['contributor']} → {c['committee_name']}")
            out(f"               Type: {c['contributor_type'] or 'unknown'}  |  "
                f"Location: {location}{employer_str}")
            out(f"               Txn: {fmt_date(c['txn_date'])}  Filed: {fmt_date(c['filed_date'])}"
                f"{lag_str}")
            tier_tag = f"  [{tier_lookup[c['race']]}]" if c.get("race") in tier_lookup else ""
            out(f"               Race: {c['race']}{tier_tag}")

    # Top expenditures by race
    expends = top_expenditures(conn, from_date, to_date)
    if expends:
        out()
        out("─" * 70)
        out("TOP EXPENDITURES (watchlist races, by amount)")
        out("─" * 70)
        for e in expends:
            purpose = e.get("purpose") or e.get("txn_subtype") or ""
            out()
            out(f"  {fmt_dollars(e['amount']):>12}  {e['committee_name']} → {e['vendor']}")
            out(f"               Purpose: {purpose}")
            tier_tag = f"  [{tier_lookup[e['race']]}]" if e.get("race") in tier_lookup else ""
            out(f"               Date: {fmt_date(e['txn_date'])}  Race: {e['race']}{tier_tag}")

    # -----------------------------------------------------------------------
    # LEGISLATIVE PAC FLOWS
    # -----------------------------------------------------------------------
    pac_in = pac_contributions_to_watchlist(conn, from_date, to_date, tier_lookup)
    pac_accumulation = caucus_pac_accumulation(conn, from_date, to_date)

    if pac_in or pac_accumulation:
        out()
        out("─" * 70)
        out("LEGISLATIVE PAC FLOWS")
        out("─" * 70)

    if pac_in:
        out()
        out("▶ PAC / COMMITTEE MONEY INTO WATCHLIST LEGISLATIVE RACES")
        for r in pac_in:
            out(f"  {fmt_dollars(r['amount']):>12}  {r['contributor']} → {r['committee_name']}")
            out(f"               Type: {r['contributor_type']}  "
                f"Txn: {fmt_date(r['txn_date'])}  Filed: {fmt_date(r['filed_date'])}")
            out(f"               Race: {r['race']}")

    if pac_accumulation:
        out()
        out("▶ CAUCUS PAC ACCUMULATION (2+ committee sources, $5,000+ total)")
        out("  Note: these PACs are not on the watchlist but are funded by")
        out("  candidate/legislative committees and may deploy into watchlist races.")
        for p in pac_accumulation:
            race_tag = f"  [{p['race']}]" if p.get("race") else "  [not on watchlist]"
            out()
            out(f"  {p['committee_name']}  (ID: {p['committee_id']}){race_tag}")
            out(f"  Total from committees: {fmt_dollars(p['total'])}  "
                f"({p['source_count']} sources)")
            for c in p["contributors"]:
                out(f"    {fmt_dollars(c['amount']):>12}  {c['contributor']}  "
                    f"(filed {fmt_date(c['filed_date'])})")

    # -----------------------------------------------------------------------
    # FLAGS
    # -----------------------------------------------------------------------
    out()
    out("=" * 70)
    out("FLAGS & ANOMALIES")
    out("=" * 70)
    any_flags = False

    # Large donations (all committees, not just watchlist)
    large = flag_large_donations(conn, from_date, to_date, large_donation)
    if large:
        any_flags = True
        out()
        out(f"▶ LARGE DONATIONS (>= {fmt_dollars(large_donation)})")
        for d in large:
            race_tag = f"  [{d['race']}]" if d.get("race") else "  [not on watchlist]"
            out(f"  {fmt_dollars(d['amount']):>12}  {d['contributor']} ({d['contributor_type'] or '?'}) "
                f"→ {d['committee_name']}")
            out(f"               {d.get('city','')}, {d.get('state','')}"
                f"  Filed: {fmt_date(d['filed_date'])}{race_tag}")

    # Late filers
    late = flag_late_filers(conn, from_date, to_date, late_days)
    if late:
        any_flags = True
        out()
        out(f"▶ LATE FILERS (>= {late_days} days between transaction and filing)")
        out(f"  Note: Oregon requires filing within 30 days (7 days in final 6 weeks before election)")
        for f in late:
            race_tag = f"  [{f['race']}]" if f.get("race") else ""
            out(f"  {f['lag_days']:2d}d lag  {fmt_dollars(f['amount'])}  "
                f"{f['contributor'] or f['committee_name']}  ({f['txn_type']})")
            out(f"             Txn: {fmt_date(f['txn_date'])}  Filed: {fmt_date(f['filed_date'])}"
                f"  Committee: {f['committee_name']}{race_tag}")

    # Employer clusters
    clusters = flag_employer_clusters(conn, from_date, to_date, cluster_count)
    if clusters:
        any_flags = True
        out()
        out(f"▶ EMPLOYER CLUSTERS ({cluster_count}+ donors from same employer, same committee)")
        for c in clusters:
            race_tag = f"  [{c['race']}]" if c.get("race") else ""
            out(f"  {c['donor_count']} donors  {fmt_dollars(c['total'])}  "
                f"Employer: {c['employer']}  → {c['committee_name']}{race_tag}")
            donors_preview = c.get("donors", "")[:120]
            out(f"    Donors: {donors_preview}")

    # Vendor concentration
    vendors = flag_vendor_concentration(conn, from_date, to_date)
    if vendors:
        any_flags = True
        out()
        out("▶ VENDOR CONCENTRATION (single vendor >= 60% of committee expenditures)")
        for v in vendors:
            out(f"  {v['pct']:.0f}% of spend  {fmt_dollars(v['vendor_total'])} / "
                f"{fmt_dollars(v['committee_total_spend'])}  "
                f"Vendor: {v['vendor']}  ← {v['committee_name']}")
            out(f"    Race: {v.get('race','—')}  ({v['n']} transactions)")

    # Out-of-state money (>= $1k to watchlist races or >= large_donation generally)
    oos = flag_out_of_state(conn, from_date, to_date, min_amount=1000)
    if oos:
        any_flags = True
        out()
        out("▶ OUT-OF-STATE CONTRIBUTIONS (>= $1,000, contributor address outside OR)")
        for o in oos:
            race_tag = f"  [{o['race']}]" if o.get("race") else ""
            out(f"  {fmt_dollars(o['amount']):>12}  {o['contributor']} ({o['contributor_type'] or '?'}) "
                f"[{o['city']}, {o['state']}]")
            out(f"               → {o['committee_name']}{race_tag}")

    # Entity type anomalies
    entities = flag_entity_anomalies(conn, from_date, to_date, large_donation)
    if entities:
        any_flags = True
        out()
        out(f"▶ LARGE NON-INDIVIDUAL CONTRIBUTIONS (>= {fmt_dollars(large_donation)}, "
            f"non-Individual contributor type)")
        for e in entities:
            race_tag = f"  [{e['race']}]" if e.get("race") else ""
            out(f"  {fmt_dollars(e['amount']):>12}  {e['contributor']} "
                f"[{e['contributor_type']}]  → {e['committee_name']}{race_tag}")
            out(f"               Location: {e.get('city','')}, {e.get('state','')}")

    if not any_flags:
        out()
        out("  No flags triggered for this period.")

    # Notable non-watchlist committees
    notable = notable_non_watchlist_committees(conn, from_date, to_date, large_donation)
    if notable:
        out()
        out("─" * 70)
        out(f"NOTABLE NON-WATCHLIST COMMITTEES (>= {fmt_dollars(large_donation)} contributions this period)")
        out("Consider adding active ones to watchlist.yaml")
        out("─" * 70)
        for n in notable:
            out(f"  ID {n['committee_id']:>6}  {fmt_dollars(n['contributions'])} raised  "
                f"{fmt_dollars(n['expenditures'])} spent  "
                f"({n['n']} txns)  {n['committee_name']}")

    out()
    out("=" * 70)
    out("END OF SUMMARY")
    out("=" * 70)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Analyze Oregon campaign finance DB")
    p.add_argument("--days", type=int, default=7,
                   help="Number of days back to analyze (default: 7)")
    p.add_argument("--from", dest="from_date", metavar="MM/DD/YYYY",
                   help="Filed-date range start")
    p.add_argument("--to",   dest="to_date",   metavar="MM/DD/YYYY",
                   help="Filed-date range end (default: today)")
    p.add_argument("--db",        default=str(DEFAULT_DB),
                   help=f"SQLite database path (default: {DEFAULT_DB})")
    p.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST),
                   help=f"Watchlist YAML path (default: {DEFAULT_WATCHLIST})")
    p.add_argument("--output", metavar="PATH",
                   help="Write report to file instead of stdout")
    return p.parse_args()


def main():
    args = parse_args()

    today = date.today()
    to_dt   = args.to_date   or today.strftime("%Y-%m-%d")
    from_dt = args.from_date or (today - timedelta(days=args.days)).strftime("%Y-%m-%d")

    # Accept MM/DD/YYYY input, normalize to YYYY-MM-DD for DB queries
    def normalize(s):
        try:
            return datetime.strptime(s, "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            return s

    from_dt = normalize(from_dt)
    to_dt   = normalize(to_dt)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        print("Run collector.py first.", file=sys.stderr)
        sys.exit(1)

    watchlist_path = Path(args.watchlist)
    thresholds  = load_thresholds(watchlist_path)
    tier_lookup = load_tier_lookup(watchlist_path)
    conn = sqlite3.connect(db_path)

    report = build_report(conn, from_dt, to_dt, thresholds, tier_lookup)
    conn.close()

    if args.output:
        Path(args.output).write_text(report)
        print(f"Report written to {args.output}")
    else:
        print(report)


def pac_contributions_to_watchlist(conn, from_date: str, to_date: str,
                                    tier_lookup: dict = None, min_amount: int = 1000) -> list:
    """
    Political-committee contributions received by watchlist legislative committees.
    Uses tier_lookup to restrict to legislative-tier race labels; falls back to
    all watchlist committees if tier_lookup is absent.
    """
    if tier_lookup:
        legislative_races = [label for label, t in tier_lookup.items() if t == "legislative"]
    else:
        legislative_races = []

    if legislative_races:
        placeholders = ",".join("?" * len(legislative_races))
        params = (from_date, to_date, min_amount, *legislative_races)
        race_clause = f"AND race IN ({placeholders})"
    else:
        params = (from_date, to_date, min_amount)
        race_clause = "AND race IS NOT NULL"

    return q(conn, f"""
        SELECT committee_id, committee_name, contributor, contributor_type,
               amount, txn_date, filed_date, race
        FROM transactions
        WHERE filed_date BETWEEN ? AND ?
          AND txn_type = 'contribution'
          AND amount >= ?
          AND (contributor_type LIKE '%Committee%' OR contributor_type LIKE '%PAC%')
          {race_clause}
        ORDER BY amount DESC
        LIMIT 20
    """, params)


def caucus_pac_accumulation(conn, from_date: str, to_date: str,
                             min_sources: int = 2, min_total: int = 5000) -> list:
    """
    PACs that received large inflows from multiple political-committee sources —
    the Future PAC pattern. Returns each qualifying PAC with its contributor list.
    """
    summary = q(conn, """
        SELECT committee_id, committee_name,
               COUNT(DISTINCT contributor) AS source_count,
               SUM(amount)                AS total,
               race
        FROM transactions
        WHERE filed_date BETWEEN ? AND ?
          AND txn_type = 'contribution'
          AND (contributor_type LIKE '%Committee%' OR contributor_type LIKE '%PAC%')
          AND amount >= 1000
        GROUP BY committee_id
        HAVING source_count >= ? AND total >= ?
        ORDER BY total DESC
        LIMIT 15
    """, (from_date, to_date, min_sources, min_total))

    results = []
    for s in summary:
        contributors = q(conn, """
            SELECT contributor, amount, txn_date, filed_date
            FROM transactions
            WHERE committee_id = ?
              AND filed_date BETWEEN ? AND ?
              AND txn_type = 'contribution'
              AND (contributor_type LIKE '%Committee%' OR contributor_type LIKE '%PAC%')
              AND amount >= 1000
            ORDER BY amount DESC
        """, (s["committee_id"], from_date, to_date))
        results.append({**s, "contributors": contributors})
    return results


if __name__ == "__main__":
    main()
