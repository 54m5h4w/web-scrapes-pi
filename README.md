# Web Scrapes Pi

## Overview

This project is a centralised scraper framework designed to collect, standardise, and store operational and event-based data for hospitality venues.

It enables:

* Automated data scraping from multiple sources
* Standardised JSON output across all datasets
* Structured storage in S3 (raw, latest, logs)
* Version-controlled deployment via GitHub
* Automated execution via Raspberry Pi (cron)

---

## Project Architecture

PC → GitHub → Raspberry Pi → S3 → CloudFront (future UI)

---

## Folder Structure

```
scrapers/   → Individual scraper scripts (one per data source)
common/     → Shared utilities (AWS, schema, logging, S3 paths)
config/     → Dataset and access definitions
docs/       → Project documentation
samples/    → Example JSON outputs
```

---

## S3 Data Structure

All data is stored in S3 using access-based partitioning:

```
{access_level}/
  raw/{dataset}/
  latest/{dataset}.json
  logs/{dataset}/
```

### Example

```
restricted/
  raw/staff-time-off/2026-04-02T11-39-44Z.json
  latest/staff-time-off.json
  logs/staff-time-off/2026-04-02T11-39-44Z.json
```

---

## Data Model

Each scraper outputs:

1. Historical snapshot (raw)
2. Latest dataset (overwritten each run)
3. Run log (execution metadata)

---

## Record Schema (Summary)

Each record follows a standard structure including:

* Title, date, time
* Location (searchable + structured)
* Categories and audience tags
* Source and scraper metadata
* Access control object

See `docs/schema_reference.md` for full schema.

---

## Deployment Workflow

### On PC

```
edit → git add → commit → push
```

### On Raspberry Pi

```
git pull → run script (via cron)
```

---

## Environment Variables

Each script requires:

* DEPUTY_API_KEY (or source-specific key)
* DEPUTY_INSTALL
* S3_BUCKET

Stored locally in:

```
.env_event_scrape
```

This file is NOT committed to GitHub.

---

## Running Scripts

Scripts are executed via cron on the Raspberry Pi.

Example:

```
0 4,16 * * * → runs twice daily
```

Each run:

* pulls latest code from GitHub
* loads environment variables
* executes scraper
* writes data to S3
* logs execution

---

## Adding a New Scraper

1. Create new script in `scrapers/`
2. Fetch and transform source data
3. Map to standard schema
4. Build dataset wrapper
5. Upload to S3 (raw + latest)
6. Write run log
7. Test locally
8. Commit and push to GitHub
9. Pull and test on Pi
10. Add cron schedule if required

---

## Key Principles

* Consistent schema across all datasets
* Separation of raw vs latest data
* Structured logging for every run
* Access control embedded in data
* Minimal duplication via shared utilities

---

## Future Roadmap

* Additional scrapers (events, ticketing, weather, etc.)
* Master dataset aggregation
* CloudFront-based UI with search (date, location)
* Role-based access control
* Data analytics and reporting
