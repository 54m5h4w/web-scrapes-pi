# Scraper Checklist

## Overview
This checklist defines the standard process for building a new scraper in the Web Scrapes Pi project.

All scrapers must:
- follow the shared schema
- use the standard S3 structure
- include logging
- support consistent naming conventions
- be testable locally before deployment
- (if using Selenium) run both locally and on Raspberry Pi without code edits

---

## Step-by-Step Process

### 1. Define the Dataset
dataset: staff-time-off
access_level: restricted
type: staff_leave
source: Deputy
scraper: deputy-staff-leave-v1

### 2. Create the Scraper File
scrapers/
{source}_{dataset}_to_s3.py

### 3. Fetch Source Data
- API / scrape / file

### 4. Clean Data
- filter + dedupe

### 5. Map Schema
- dates YYYY-MM-DD
- times HH:MM

### 6. Location Object
{ code, search_text, lat, lon }

### 7. Access Object
{ level, dataset, roles }

### 8. Payload
- dataset wrapper

### 9. Upload to S3
raw + latest

### 10. Log
logs/{dataset}

### 11. Logging
start / count / errors

### 12. Test Locally
cd repo
set PYTHONPATH
run script

### 12A. Selenium
Must run local + Pi

### 13. Commit
git add / commit / push

### 14. Pull on Pi
git pull

### 15. Test on Pi
run manually

### 16. Cron
crontab -e

---

## Key Principles
- env vars
- portable
- test locally first
