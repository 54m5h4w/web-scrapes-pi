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

```text
scrapers/   → Individual scraper scripts (one per data source)
common/     → Shared utilities (AWS, schema, logging, S3 paths, Selenium helpers)
config/     → Dataset and access definitions
docs/       → Project documentation
samples/    → Example JSON outputs
```

---

## S3 Data Structure

All data is stored in S3 using access-based partitioning:

```text
{access_level}/
  raw/{dataset}/
  latest/{dataset}.json
  logs/{dataset}/
```

### Example

```text
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

## Event Dataset Strategy

For public event sources, each scraper should write to its own source-specific dataset rather than sharing a single `public-events.json` file.

### Source datasets

```text
public/latest/marvel-events.json
public/latest/mcec-events.json
public/latest/eventbrite-events.json
public/latest/mcg-events.json
```

### Aggregated dataset (future)

```text
public/index/master.json
```

This approach:

* keeps each source isolated for debugging and reliability
* avoids scrapers overwriting each other
* supports clean retry/idempotent runs
* enables a scalable aggregation layer for UI/search

---

## Record Schema (Summary)

Each record follows a standard structure including:

* Title, date, time
* Location (searchable + structured)
* Categories and audience tags
* Source and scraper metadata
* Access control object

The access model now supports both:

* role-based access
* venue-based access

This means visibility can be restricted by user role and by venue scope.

Example access object:

```json
"access": {
  "level": "public",
  "dataset": "mcec-events",
  "allowed_roles": ["staff", "supervisor", "manager", "admin"],
  "allowed_venues": ["ALL"]
}
---

## Deployment Workflow

### On PC

```text
edit → git add → commit → push
```

### On Raspberry Pi

```text
git pull → run script (via cron)
```

---

## Environment Variables

Each script requires:

* source-specific variables where applicable (for example `DEPUTY_API_KEY`, `DEPUTY_INSTALL`)
* `S3_BUCKET`

Browser-based/Selenium scrapers should also support:

* `SELENIUM_HEADLESS`
* `CHROME_BINARY`
* `CHROMEDRIVER_PATH`

Stored locally in:

```text
.env_event_scrape
```

This file is NOT committed to GitHub.

### Recommended Selenium variables

For local testing:

```text
SELENIUM_HEADLESS=false
```

For Raspberry Pi/Linux:

```text
export SELENIUM_HEADLESS=true
export CHROME_BINARY=/usr/bin/chromium
export CHROMEDRIVER_PATH=/usr/bin/chromedriver
```

---

## Selenium Scrapers

Some scrapers require Selenium and a local browser driver.

These scrapers must be built so the same file works in both environments:

* local PC testing
* Raspberry Pi/Linux scheduled execution

### Required behaviour

Browser-based scrapers should follow this runtime order:

1. Use explicit environment variables if provided
2. On Linux/Pi, fall back to standard Chromium/chromedriver paths
3. On local PC, fall back to Selenium Manager or installed browser discovery

### Recommended environment variables

```text
SELENIUM_HEADLESS=true|false
CHROME_BINARY=/path/to/browser
CHROMEDRIVER_PATH=/path/to/chromedriver
```

### Local testing standard

Before pushing a new Selenium scraper:

* run it locally from the project root
* confirm browser launches successfully
* confirm records are parsed correctly
* confirm JSON payload structure is valid
* confirm S3 upload and log creation if enabled

Typical local test pattern on Windows:

```text
cd <repo-root>
set PYTHONPATH=%cd%
set S3_BUCKET=event-scrape-data
set SELENIUM_HEADLESS=false
python scrapers/<script_name>.py
```

### Raspberry Pi standard

For Pi execution, use explicit browser paths in `.env_event_scrape` where possible:

```text
export SELENIUM_HEADLESS=true
export CHROME_BINARY=/usr/bin/chromium
export CHROMEDRIVER_PATH=/usr/bin/chromedriver
```

Typical Pi test pattern:

```text
source /home/admin/scripts/.env_event_scrape
PYTHONPATH=/home/admin/scripts /home/admin/venvs/event-scrape/bin/python /home/admin/scripts/scrapers/<script_name>.py
```

This avoids needing to edit scraper code when moving between local testing and Raspberry Pi deployment.

---

## Running Scripts

Scripts are executed via cron on the Raspberry Pi.

Example:

```text
0 4,16 * * * → runs twice daily
```

Each run:

* pulls latest code from GitHub
* loads environment variables
* executes scraper
* writes data to S3
* logs execution

For Selenium/browser-based scrapers, always:

* test locally first
* test manually on the Pi before adding cron
* confirm raw, latest, and log outputs are created

---

## Adding a New Scraper

1. Create new script in `scrapers/`
2. Fetch and transform source data
3. Map to standard schema
4. Build dataset wrapper
5. Upload to S3 (raw + latest)
6. Write run log
7. Test locally
8. If using Selenium, confirm cross-platform driver behaviour
9. Commit and push to GitHub
10. Pull and test on Pi
11. Add cron schedule if required

### Event scraper naming

For event scrapers, use **source-specific dataset names**:

* `marvel-events`
* `mcec-events`
* `eventbrite-events`
* `mcg-events`

Do NOT have multiple scrapers write directly to the same `public-events` dataset.

---

## Key Principles

* Consistent schema across all datasets
* Separation of raw vs latest data
* Structured logging for every run
* Access control embedded in data
* Access control supports both role and venue entitlement
* Minimal duplication via shared utilities
* Browser-based scrapers must be portable across local and Pi environments

---

## Future Roadmap

* Additional scrapers (events, ticketing, weather, etc.)
* Master dataset aggregation (`public/index/master.json`)
* CloudFront-based UI with search (date, location)
* Role-based access control
* Data analytics and reporting
* Shared Selenium/browser helpers in `common/`

## Access Model

Access is controlled at two levels:

1. **Access level**
   * `public`
   * `internal`
   * `restricted`

2. **Access entitlement**
   * `allowed_roles`
   * `allowed_venues`

### Roles

Typical role values include:

* `staff`
* `supervisor`
* `manager`
* `admin`

### Venues

Current venue values:

* `BP`
* `P5`
* `HATF`
* `ALL`

### User access example

A user record may look like:

```json
{
  "username": "sam",
  "role": "admin",
  "venue": "ALL",
  "display_name": "Sam"
}

A venue-specific user may look like:

{
  "username": "hatf_staff",
  "role": "staff",
  "venue": "HATF",
  "display_name": "HATF Staff"
}

A multi-venue user may look like:

{
  "username": "ops_manager",
  "role": "manager",
  "venue": ["P5", "BP"],
  "display_name": "Operations Manager"
}