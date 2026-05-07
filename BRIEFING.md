# Oregon Campaign Finance Pipeline — Project Briefing

## What This Is

A data pipeline that automatically collects Oregon campaign finance data from
the state's official system (ORESTAR), stores it in a local database, and
generates editorial digests for a local government accountability publication.

This is the engineering counterpart to an existing meeting-minutes digest
system. Same editorial mission — surface what matters to engaged residents —
but applied to money in politics rather than government documents.

---

## Data Source: ORESTAR

- **URL:** https://secure.sos.state.or.us/orestar/gotoPublicTransactionSearch.do
- **What it is:** Oregon Secretary of State's campaign finance filing system
- **Technology:** Java servlet app (`.do` URLs), HTML form POST requests
- **No public API.** No bulk download. Data must be collected via form
  submission and CSV export.
- **Export path:** Search results pages include a CSV/Excel export button that
  submits a secondary form action. This is the extraction mechanism — not
  HTML table scraping.

### Key facts about Oregon campaign finance law
- Transactions must be filed within **30 calendar days** of the transaction date
- In the **final 6 weeks before an election**, the deadline shortens to **7 days**
- Oregon has **no contribution limits** — any size donation is legal
- All filed transactions are immediately public

### ORESTAR search filters available
- Transaction date range OR filed date range
- Transaction type: Contribution / Expenditure / Other
- Contributor/payee name, type, employer, occupation, city/state
- Committee name or ID
- Amount range
- Independent expenditure flag

---

## Architecture

Three layers:

```
COLLECTOR (Python) --> DATA STORE (SQLite) --> DIGEST ENGINE (Claude API)
```

### Layer 1: Collector

A Python script that:
1. POSTs to ORESTAR's transaction search form with a date filter
   (e.g., `transactionFiledDate` = yesterday or a rolling window)
2. Triggers the CSV export from the results page
3. Parses the CSV into structured records
4. Writes to SQLite, deduplicating on ORESTAR's own transaction IDs

**Scheduling:** GitHub Actions on a daily cron (free, auditable, no server
needed). Also triggered manually around Oregon's key reporting deadlines.

**Scraping etiquette:** Add delays between requests. Oregon SOS doesn't
publish scraping terms. The Portland Record ran daily pulls from 2022–2024
without apparent issue — that's our proof of concept.

### Layer 2: Data Store

SQLite database. Schema:

```sql
CREATE TABLE transactions (
  txn_id           TEXT PRIMARY KEY,   -- ORESTAR's own transaction ID
  committee_id     TEXT,
  committee_name   TEXT,
  txn_type         TEXT,               -- contribution, expenditure, etc.
  txn_subtype      TEXT,
  purpose          TEXT,
  amount           REAL,
  txn_date         DATE,
  filed_date       DATE,
  contributor      TEXT,
  contributor_type TEXT,               -- Individual, Business, PAC, etc.
  employer         TEXT,
  occupation       TEXT,
  city             TEXT,
  state            TEXT,
  race             TEXT,               -- editorial tag (see watchlist)
  first_seen       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

The `race` field is an editorial layer — a separate config file maps
committee IDs to human-readable race names (e.g., "Portland City Council
Ward 2", "Multnomah County Chair"). Editors update this config without
touching code.

### Layer 3: Digest Engine

Does NOT feed raw data to Claude. Instead:
1. A pre-analysis script computes aggregates and flags from the database
2. A structured data summary is assembled as text
3. That summary is sent to the Claude API with an editorial prompt
4. Claude produces a formatted digest

This separation means Claude interprets and editorializes — it doesn't
do arithmetic on raw rows.

---

## Editorial Flags to Compute (Pre-Analysis Layer)

These are the accountability signals the pre-analysis script should detect
and surface to Claude:

| Flag | What to detect |
|------|----------------|
| Late filers | Transactions reported close to the 30-day limit |
| Employer clustering | Multiple donors from the same employer in a short window |
| Vendor concentration | Candidate spending heavily with a single vendor |
| Out-of-state money | Contributions from outside Oregon |
| Entity type anomalies | Large contributions from LLCs or non-individual entities |
| New committees | Committees registered this week not previously in watchlist |
| Timing flags | Large donations right before a key vote or filing deadline |
| Cycle comparison | Current period totals vs. same period in prior election cycle |

---

## Watchlist / Scope

For a Portland/Multnomah County focused publication, the recommended scope is:

- **Race-specific pulls:** Maintain a `watchlist.yaml` of committee IDs for
  active local races. Pull all transactions for those committees.
- **Threshold alerts:** Also catch any large donations (e.g., >$5,000) to
  any Oregon committee — for statewide context and unexpected entrants.

Example `watchlist.yaml` structure:
```yaml
races:
  - id: "12345"
    name: "Portland City Council Ward 2"
    cycle: 2026
  - id: "67890"
    name: "Multnomah County Chair"
    cycle: 2026
thresholds:
  large_donation: 5000
  large_expenditure: 10000
```

---

## Digest Prompt Structure (Claude API)

The prompt to Claude should be structured as:

```
[SYSTEM] Editorial mission and output format instructions

[DATA BLOCK] Pre-computed summary:
  - Period covered
  - Races covered
  - Contributions table (by race, top donors, new donors)
  - Expenditures table (by race, top vendors)
  - Flags list (computed by pre-analysis script)
  - New committee registrations

[USER] Produce a campaign finance digest for [period].
       Follow this format: [template]
```

---

## Output Format (Digest Template)

```
### OREGON CAMPAIGN FINANCE — [JURISDICTION] — Week of [DATE]

**One-sentence summary of the most significant money movement this period.**

---

#### 💰 Top Contributions
2–5 notable contributions or contribution patterns.
- Committee name / race
  - Amount, donor name, donor type/employer
  - Date filed vs. transaction date
  - Why it matters (one sentence)

---

#### 💸 Notable Expenditures
2–4 expenditures worth flagging.
- Committee name / race
  - Amount, vendor, purpose
  - Why it matters (one sentence)

---

#### 🚩 Flags & Anomalies
Items that warrant closer editorial scrutiny.
- Flag type, description, amounts/names involved

---

#### 🧵 Threads to Pull
2–3 specific follow-up angles for the editor.

---

#### ⚙️ Data Notes
Filing lag reminders, missing data, caveats.
```

---

## Build Order

1. **Scraper first** — validate the ORESTAR POST request and CSV export
   before building anything else. Start with a single test request for the
   last 7 days of filed transactions and inspect the raw response.
2. **Database + dedup logic** — once CSV structure is confirmed
3. **Watchlist config** — simple YAML, manually maintained by editors
4. **Pre-analysis aggregator** — compute flags and summaries from DB
5. **Claude API digest prompt** — iterate on output quality
6. **GitHub Actions workflow** — automate daily runs + email delivery

---

## Prior Art

- **Portland Record "Campaign Funderator"** — ran daily ORESTAR pulls
  2022–2024, published daily contribution and expenditure digests.
  Proof the scraping approach is stable over time.
- **HackOregon "Behind The Curtain"** — older API project that wrapped
  ORESTAR data. May be defunct but worth checking for any surviving
  documentation on the form parameters.

---

## Related Project Context

This pipeline is the campaign finance counterpart to a meeting-minutes
digest system for local government accountability journalism. Same
publication, same editorial mission, parallel architecture. The meeting
minutes system handles unstructured documents; this system handles
structured time-series data. They should eventually feed the same
editorial workflow.
