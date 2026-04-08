# Schema Reference

## Overview

This document defines the shared JSON schema used across all scrapers in the Web Scrapes Pi project.

All datasets must use:

- a standard dataset wrapper
- a consistent record structure
- a structured location object
- a structured access object

This ensures all scraper outputs can be consumed consistently by S3 storage, future aggregation, CloudFront, and UI filtering. The schema is designed so each scraper can map source-specific data into one shared format. :contentReference[oaicite:2]{index=2}

---

## Dataset Wrapper

Each scraper must output a top-level dataset payload in this structure:

```json
{
  "dataset": "mcec-events",
  "source": "MCEC",
  "source_url": "https://www.mcec.com.au/whats-on",
  "type": "public_event",
  "scraper": "mcec-whats-on-v1",
  "access_level": "public",
  "allowed_roles": ["staff", "supervisor", "manager", "admin"],
  "scraped_at": "2026-04-08T09:15:00Z",
  "record_count": 25,
  "records": []
}