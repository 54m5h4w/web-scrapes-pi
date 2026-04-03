# Scraper Checklist

## Overview

This checklist defines the standard process for building a new scraper in the Web Scrapes Pi project.

All scrapers must:

* follow the shared schema
* use the standard S3 structure
* include logging
* support consistent naming conventions

---

## Step-by-Step Process

### 1. Define the Dataset

Before writing code, define:

* Dataset name (kebab-case)
* Access level (`public`, `internal`, `restricted`)
* Record type (snake_case)
* Source name
* Scraper name (kebab-case + version)

Example:

```
dataset: staff-time-off
access_level: restricted
type: staff_leave
source: Deputy
scraper: deputy-staff-leave-v1
```

---

### 2. Create the Scraper File

Create a new file in:

```
scrapers/
```

Naming convention:

```
{source}_{dataset}_to_s3.py
```

Examples:

```
deputy_staff_time_off_to_s3.py
mcec_events_to_s3.py
eventbrite_events_to_s3.py
```

---

### 3. Fetch Source Data

* Call API, scrape webpage, or load file
* Handle pagination if required
* Handle API errors gracefully

---

### 4. Clean and Filter Data

* Remove irrelevant records
* Filter by:

  * date range
  * company/location
  * active records only
* Deduplicate records where needed

---

### 5. Map to Record Schema

Transform source data into the standard record format.

Ensure:

* All required fields are present
* Dates are in `YYYY-MM-DD`
* Times are `HH:MM` or `null`
* Arrays are always arrays (never strings)

---

### 6. Build Location Object

Each record must include a structured location object:

```
{
  "code": "...",
  "search_text": "...",
  "latitude": null,
  "longitude": null
}
```

Use:

* predefined mappings where possible
* consistent codes across datasets

---

### 7. Add Access Object

Every record must include:

```
"access": {
  "level": "...",
  "dataset": "...",
  "allowed_roles": [...]
}
```

This must match the dataset definition.

---

### 8. Build Wrapper Payload

Wrap all records into the standard dataset structure:

* dataset
* source
* type
* scraper
* access_level
* allowed_roles
* scraped_at
* record_count
* records

---

### 9. Upload to S3

Each run must write:

### Raw file

```
{access_level}/raw/{dataset}/{timestamp}.json
```

### Latest file

```
{access_level}/latest/{dataset}.json
```

---

### 10. Write Run Log

Each run must create a log file:

```
{access_level}/logs/{dataset}/{timestamp}.json
```

Include:

* status (`ok` or `error`)
* timestamps (start + finish)
* record counts
* S3 keys written
* error message (if any)

---

### 11. Add Logging

Include logging for:

* start of run
* number of records fetched
* number of records processed
* upload success
* errors

---

### 12. Test Locally (PC)

Run script locally with environment variables:

* confirm output structure
* confirm S3 upload
* confirm logs created

---

### 13. Commit to GitHub

```
git add .
git commit -m "Add {dataset} scraper"
git push
```

---

### 14. Pull on Raspberry Pi

```
cd /home/admin/scripts
git pull
```

---

### 15. Test on Pi

Run manually:

```
source /home/admin/scripts/.env_event_scrape
PYTHONPATH=/home/admin/scripts /home/admin/venvs/event-scrape/bin/python /home/admin/scripts/scrapers/{script}.py
```

Confirm:

* S3 files created
* logs written
* no errors

---

### 16. Add Cron Job (if required)

Edit cron:

```
crontab -e
```

Add job:

```
0 4,16 * * * /bin/bash -lc 'cd /home/admin/scripts && git pull && source /home/admin/scripts/.env_event_scrape && PYTHONPATH=/home/admin/scripts /home/admin/venvs/event-scrape/bin/python /home/admin/scripts/scrapers/{script}.py' >> /home/admin/logs/{dataset}.log 2>&1
```

---

## Naming Conventions

### Dataset

* lowercase
* kebab-case

Example:

```
staff-time-off
public-events
```

---

### Type

* lowercase
* snake_case

Example:

```
staff_leave
public_event
```

---

### Scraper

* lowercase
* kebab-case
* version suffix

Example:

```
deputy-staff-leave-v1
mcec-events-v1
```

---

## Key Principles

* Do not hardcode credentials
* Always use environment variables
* Keep schema consistent across all scrapers
* Keep logic modular where possible
* Prefer reusable functions over duplication
* Ensure idempotent runs (safe to run repeatedly)
* Always log outputs and errors

---

## Common Mistakes to Avoid

* Missing access object
* Incorrect date formats
* Using strings instead of arrays
* Not writing to both raw and latest
* Not logging runs
* Hardcoding values instead of using config
* Forgetting to update cron path after restructuring

---

## Future Enhancements

* Move shared logic into `common/` modules
* Introduce dataset configuration files
* Build aggregation scripts for `index/`
* Add alerting on failed runs
* Add validation checks for schema compliance
