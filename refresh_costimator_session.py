#!/usr/bin/env python3
"""
Refresh Costimator session cookies and store them in AWS SSM.

Flow:
1. Load existing Costimator session from SSM.
2. Validate it against BP / HATF / P5 Costimator cost centres.
3. If valid, exit successfully.
4. If invalid/expired, log in with username/password stored in SSM.
5. Validate the fresh login session.
6. Save the fresh session_id + csrftoken back to /costimator/alpha/session.

Required SSM parameters:
- /costimator/alpha/session      SecureString JSON, written by this script
- /costimator/alpha/username     SecureString
- /costimator/alpha/password     SecureString

Optional environment variables:
- AWS_REGION                         default: ap-southeast-2
- AWS_PROFILE                        optional profile name when running manually
- COSTIMATOR_PARAM                   default: /costimator/alpha/session
- COSTIMATOR_USERNAME_PARAM          default: /costimator/alpha/username
- COSTIMATOR_PASSWORD_PARAM          default: /costimator/alpha/password
- COSTIMATOR_FORCE_LOGIN             true/1/yes to skip existing-session check
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

import boto3
import requests


AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
AWS_PROFILE = os.environ.get("AWS_PROFILE") or os.environ.get("AWS_PROFILE_NAME")

PARAM_NAME = os.environ.get("COSTIMATOR_PARAM", "/costimator/alpha/session")
USERNAME_PARAM = os.environ.get("COSTIMATOR_USERNAME_PARAM", "/costimator/alpha/username")
PASSWORD_PARAM = os.environ.get("COSTIMATOR_PASSWORD_PARAM", "/costimator/alpha/password")

DOMAIN = os.environ.get("COSTIMATOR_DOMAIN", "alpha.costimator.com.au")
BASE_URL = f"https://{DOMAIN}"
LOGIN_URL = f"{BASE_URL}/accounts/login/"

# Cost centres confirmed:
# BP   = 721
# HATF = 725
# P5   = 729
CHECKS = [
    ("bp", 721),
    ("hatf", 725),
    ("p5", 729),
]

TIMEOUT = 30


class CostimatorAuthError(RuntimeError):
    """Raised when Costimator rejects the current session/login."""


def now_z():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message):
    print(f"[{now_z()}] {message}", flush=True)


def aws_ssm_client():
    if AWS_PROFILE:
        session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    else:
        session = boto3.Session(region_name=AWS_REGION)
    return session.client("ssm")


def get_secure_param(ssm, name):
    response = ssm.get_parameter(Name=name, WithDecryption=True)
    return response["Parameter"]["Value"]


def put_secure_json(ssm, name, data):
    ssm.put_parameter(
        Name=name,
        Type="SecureString",
        Value=json.dumps(data, ensure_ascii=False),
        Overwrite=True,
    )


def load_session_data(ssm):
    response = ssm.get_parameter(Name=PARAM_NAME, WithDecryption=True)
    data = json.loads(response["Parameter"]["Value"])

    session_id = data.get("session_id") or data.get("sessionid")
    csrftoken = data.get("csrftoken") or data.get("csrf_token")

    if not session_id or not csrftoken:
        raise CostimatorAuthError("SSM parameter is missing session_id/sessionid or csrftoken.")

    # Normalise keys so downstream code is consistent.
    data["session_id"] = session_id
    data["csrftoken"] = csrftoken
    return data


def cookie_value(session, name, fallback=None):
    value = fallback
    for cookie in session.cookies:
        if cookie.name == name and cookie.value:
            value = cookie.value
    return value


def new_http_session():
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 CostimatorSessionRefresh/2.0",
            "Accept-Language": "en-AU,en;q=0.9",
        }
    )
    return session


def session_from_saved_data(data):
    session = new_http_session()

    # Store cookies without a domain so requests will send them to alpha.costimator.com.au.
    session.cookies.set("sessionid", data["session_id"])
    session.cookies.set("csrftoken", data["csrftoken"])

    return session


def is_login_response(response):
    final_url = (response.url or "").lower()
    text = response.text or ""

    return (
        "/accounts/login" in final_url
        or 'name="password"' in text
        or 'id="id_password"' in text
        or "csrfmiddlewaretoken" in text and "Log in" in text
    )


def request_json(session, url, csrftoken):
    headers = {
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRFToken": csrftoken,
        "Referer": f"{BASE_URL}/",
    }

    response = session.get(url, headers=headers, allow_redirects=True, timeout=TIMEOUT)

    if response.status_code in (401, 403):
        raise CostimatorAuthError(f"HTTP {response.status_code} for {url}")

    if is_login_response(response):
        raise CostimatorAuthError(f"Redirected to login for {url}")

    if response.status_code != 200:
        body = (response.text or "")[:500].replace("\n", " ")
        raise RuntimeError(f"HTTP {response.status_code} for {url}: {body}")

    try:
        return response.json()
    except ValueError as exc:
        body = (response.text or "")[:500].replace("\n", " ")
        raise RuntimeError(f"Expected JSON from {url}, got non-JSON response: {body}") from exc


def payload_records(data):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("results", "data", "items", "objects"):
            value = data.get(key)
            if isinstance(value, list):
                return value

    return []


def validate_http_session(session, csrftoken):
    results = []

    for venue_code, cost_centre_id in CHECKS:
        url = (
            f"{BASE_URL}/cost-centre/{cost_centre_id}/rad-api/tfp.menu/"
            "?noPagination=True&hideArchived=True"
        )

        data = request_json(session, url, csrftoken)
        records = payload_records(data)

        results.append(
            {
                "venue_code": venue_code,
                "cost_centre_id": cost_centre_id,
                "record_count": len(records),
            }
        )

        log(f"{venue_code} cost centre {cost_centre_id} auth OK; records={len(records)}")

    return results


def validate_saved_session_data(data):
    session = session_from_saved_data(data)
    return validate_http_session(session, data["csrftoken"])


def extract_csrf_from_login_html(html):
    match = re.search(r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']([^"\']+)["\']', html)
    if not match:
        # Some templates output attributes in the reverse order.
        match = re.search(r'value=["\']([^"\']+)["\']\s+name=["\']csrfmiddlewaretoken["\']', html)

    if not match:
        raise RuntimeError("Could not find csrfmiddlewaretoken on Costimator login page.")

    return match.group(1)


def login_to_costimator(ssm):
    username = get_secure_param(ssm, USERNAME_PARAM)
    password = get_secure_param(ssm, PASSWORD_PARAM)

    if not username or not password:
        raise RuntimeError("Costimator username/password SSM parameters are empty.")

    session = new_http_session()

    log("Fetching Costimator login page.")
    login_page = session.get(LOGIN_URL, timeout=TIMEOUT)
    login_page.raise_for_status()

    csrf = extract_csrf_from_login_html(login_page.text)
    csrf_cookie = cookie_value(session, "csrftoken", csrf)

    log("Posting Costimator login form.")
    response = session.post(
        LOGIN_URL,
        data={
            "csrfmiddlewaretoken": csrf,
            "username": username,
            "password": password,
            "next": "",
        },
        headers={
            "Referer": LOGIN_URL,
            "X-CSRFToken": csrf_cookie,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        allow_redirects=True,
        timeout=TIMEOUT,
    )

    response.raise_for_status()

    if is_login_response(response):
        raise CostimatorAuthError(
            "Costimator login returned the login page again. Check username/password or account access."
        )

    session_id = cookie_value(session, "sessionid")
    csrftoken = cookie_value(session, "csrftoken", csrf_cookie or csrf)

    if not session_id:
        raise CostimatorAuthError(
            f"Costimator login did not return a sessionid cookie. Final URL: {response.url}"
        )

    return session, {
        "session_id": session_id,
        "csrftoken": csrftoken,
        "updated_at": now_z(),
        "source": "pi-requests-login-refresh",
        "domain": DOMAIN,
        "username_param": USERNAME_PARAM,
    }


def force_login_enabled():
    return str(os.environ.get("COSTIMATOR_FORCE_LOGIN", "")).strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
    )


def main():
    ssm = aws_ssm_client()
    force_login = force_login_enabled()

    if not force_login:
        try:
            log(f"Loading existing Costimator session from {PARAM_NAME}.")
            existing = load_session_data(ssm)
            results = validate_saved_session_data(existing)
            log("Existing Costimator session is valid. No refresh needed.")
            log(f"Validation summary: {json.dumps(results, ensure_ascii=False)}")
            return 0

        except CostimatorAuthError as exc:
            log(f"Existing Costimator session is invalid/expired: {exc}")
            log("Attempting fresh Costimator login.")

    else:
        log("COSTIMATOR_FORCE_LOGIN is enabled. Skipping existing-session check.")

    login_session, fresh_data = login_to_costimator(ssm)

    log("Validating fresh Costimator login session.")
    results = validate_http_session(login_session, fresh_data["csrftoken"])

    fresh_data["validated_at"] = now_z()
    fresh_data["checks"] = results

    put_secure_json(ssm, PARAM_NAME, fresh_data)

    log(f"Updated {PARAM_NAME} with fresh Costimator session.")
    log(f"Validation summary: {json.dumps(results, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(f"ERROR: {exc}")
        raise
