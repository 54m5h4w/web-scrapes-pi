# Schema Reference

## Overview

All scrapers in this project should output data using a shared JSON structure.

This ensures:

* consistent S3 output
* easier aggregation across datasets
* predictable CloudFront/UI integration
* simpler filtering by date, location, source, and access level
* faster development of future scrapers

There are two schema layers:

1. **Record schema** → each individual item in the dataset
2. **Wrapper schema** → the full dataset file written to S3

---

## Record Schema

Each record should follow this structure:

```json
{
  "title": "string",
  "date": "YYYY-MM-DD",
  "day_name": "string",
  "start_time": "HH:MM|null",
  "end_time": "HH:MM|null",
  "location_name": "string",
  "location": {
    "code": "string",
    "search_text": "string",
    "latitude": "number|null",
    "longitude": "number|null"
  },
  "categories": ["string"],
  "audience_type": ["string"],
  "source": "string",
  "source_url": "string|null",
  "type": "string",
  "scraper": "string",
  "notes": "string",
  "access": {
    "level": "public|internal|restricted",
    "dataset": "string",
    "allowed_roles": ["staff", "supervisor", "manager", "admin"]
  }
}
```

---

## Field Definitions

### `title`

Human-readable title for the record.

Examples:

* `Jane Smith Annual Leave`
* `Melbourne Food Expo`
* `Hotel Investment Forum`

---

### `date`

Primary date for the record in ISO format.

Format:

```text
YYYY-MM-DD
```

Example:

```json
"date": "2026-04-14"
```

---

### `day_name`

Day of week derived from `date`.

Examples:

* `Monday`
* `Tuesday`
* `Sunday`

---

### `start_time`

Start time in 24-hour format.

Format:

```text
HH:MM
```

Example:

```json
"start_time": "14:00"
```

If not relevant or unknown:

```json
"start_time": null
```

---

### `end_time`

End time in 24-hour format.

Format:

```text
HH:MM
```

Example:

```json
"end_time": "17:30"
```

If not relevant or unknown:

```json
"end_time": null
```

---

### `location_name`

Human-readable name for the location or venue.

Examples:

* `BangPop`
* `Henry and the Fox`
* `Plus 5`
* `MCEC`
* `All Venues`

---

### `location`

Structured location object used for searching and future map support.

```json
{
  "code": "BP",
  "search_text": "BangPop BP South Wharf Melbourne",
  "latitude": null,
  "longitude": null
}
```

#### `location.code`

Short stable location identifier.

Examples:

* `BP`
* `HATF`
* `P5`
* `MCEC`
* `ALL`

#### `location.search_text`

Freeform searchable string used by future UI search.

Examples:

* `BangPop BP South Wharf Melbourne`
* `Henry and the Fox HATF Melbourne CBD`

#### `location.latitude`

Decimal latitude or `null`.

#### `location.longitude`

Decimal longitude or `null`.

---

### `categories`

Array of tags describing the type or theme of the record.

Examples:

```json
["Staff Time Off"]
```

```json
["Trade Event", "Food", "Exhibition"]
```

Always stored as an array.

---

### `audience_type`

Array of tags describing who the record is relevant to.

Examples:

```json
["Internal", "Staffing"]
```

```json
["Public", "Hospitality"]
```

```json
["Trade", "Hotel"]
```

Always stored as an array.

---

### `source`

The original source system or platform the data comes from.

Examples:

* `Deputy`
* `MCEC`
* `Eventbrite`
* `Manual Import`

---

### `source_url`

The original source page or URL the record was scraped from.

Examples:

```json
"source_url": "https://mcec.com.au/whats-on/example-event"
```

If not applicable:

```json
"source_url": null
```

---

### `type`

High-level machine-friendly record type.

Use snake_case.

Examples:

* `staff_leave`
* `public_event`
* `trade_event`
* `historical_sales`
* `booking`

---

### `scraper`

Identifier for the script/process that created the record.

Use kebab-case and version suffix.

Examples:

* `deputy-staff-leave-v1`
* `mcec-events-v1`
* `eventbrite-events-v1`

---

### `notes`

Free-text notes or comments.

Use:

* empty string if there are no notes
* short useful comments where needed

Examples:

* `Multi-day leave expanded to one row per day`
* `Dates inferred from source page`

---

### `access`

Object that defines the intended access level for the record.

```json
{
  "level": "restricted",
  "dataset": "staff-time-off",
  "allowed_roles": ["manager", "admin"]
}
```

#### `access.level`

One of:

* `public`
* `internal`
* `restricted`

#### `access.dataset`

Machine-friendly dataset name in kebab-case.

Examples:

* `staff-time-off`
* `public-events`
* `trade-events`
* `historical-sales`

#### `access.allowed_roles`

Array of roles allowed to access the dataset.

Valid roles:

* `staff`
* `supervisor`
* `manager`
* `admin`

---

## Wrapper Schema

Each dataset file written to S3 should follow this structure:

```json
{
  "dataset": "string",
  "source": "string",
  "source_url": "string|null",
  "type": "string",
  "scraper": "string",
  "access_level": "public|internal|restricted",
  "allowed_roles": ["staff", "supervisor", "manager", "admin"],
  "scraped_at": "YYYY-MM-DDTHH:MM:SSZ",
  "record_count": 0,
  "records": []
}
```

---

## Wrapper Field Definitions

### `dataset`

Machine-friendly dataset name.

Example:

```json
"dataset": "staff-time-off"
```

---

### `source`

Source system/platform name.

Example:

```json
"source": "Deputy"
```

---

### `source_url`

Primary source URL for dataset if relevant.

Example:

```json
"source_url": null
```

---

### `type`

Machine-friendly record type.

Example:

```json
"type": "staff_leave"
```

---

### `scraper`

Identifier for the scraper that generated the file.

Example:

```json
"scraper": "deputy-staff-leave-v1"
```

---

### `access_level`

Top-level access class for the dataset.

Examples:

* `public`
* `internal`
* `restricted`

---

### `allowed_roles`

Roles allowed to access the dataset.

Example:

```json
["manager", "admin"]
```

---

### `scraped_at`

UTC timestamp showing when the dataset file was generated.

Format:

```text
YYYY-MM-DDTHH:MM:SSZ
```

Example:

```json
"scraped_at": "2026-04-02T09:46:14Z"
```

---

### `record_count`

Number of records in the dataset.

Example:

```json
"record_count": 124
```

---

### `records`

Array of record objects using the standard record schema.

---

## Example: Staff Leave Record

```json
{
  "title": "Jane Smith Annual Leave",
  "date": "2026-04-14",
  "day_name": "Tuesday",
  "start_time": null,
  "end_time": null,
  "location_name": "BangPop",
  "location": {
    "code": "BP",
    "search_text": "BangPop BP South Wharf Melbourne",
    "latitude": null,
    "longitude": null
  },
  "categories": ["Staff Time Off"],
  "audience_type": ["Internal", "Staffing"],
  "source": "Deputy",
  "source_url": null,
  "type": "staff_leave",
  "scraper": "deputy-staff-leave-v1",
  "notes": "",
  "access": {
    "level": "restricted",
    "dataset": "staff-time-off",
    "allowed_roles": ["manager", "admin"]
  }
}
```

---

## Example: Dataset Wrapper

```json
{
  "dataset": "staff-time-off",
  "source": "Deputy",
  "source_url": null,
  "type": "staff_leave",
  "scraper": "deputy-staff-leave-v1",
  "access_level": "restricted",
  "allowed_roles": ["manager", "admin"],
  "scraped_at": "2026-04-02T09:46:14Z",
  "record_count": 124,
  "records": []
}
```

---

## Naming Rules

### Dataset names

Use:

* lowercase
* kebab-case

Examples:

* `staff-time-off`
* `public-events`

### Record types

Use:

* lowercase
* snake_case

Examples:

* `staff_leave`
* `public_event`

### Scraper names

Use:

* lowercase
* kebab-case
* version suffix

Examples:

* `deputy-staff-leave-v1`
* `mcec-events-v1`

---

## Design Principles

* All scrapers should map to the same schema
* Unknown values should use `null` where appropriate
* Tags should always be arrays
* Access metadata should always be included
* Searchable location data should always be structured
* The schema should support future UI filtering, aggregation, and role-aware delivery
