"""
salesforce_io.py
Salesforce REST helpers used by the hygiene job.

Lead describe supplies picklist catalogs. Query/PATCH can be added later;
CSV remains the default lead store.
"""

import json
import os
import ssl
import urllib.error
import urllib.request

API_VERSION = "v61.0"
PICKLIST_FIELD_NAMES = (
    "Industry", "LeadSource", "Status", "Country", "State",
    "CountryCode", "StateCode",
)


class SalesforceNotConnected(Exception):
    """Credentials are missing."""


class SalesforceApiError(Exception):
    """The Salesforce API rejected the call."""


def instance_url() -> str:
    return (os.environ.get("SALESFORCE_INSTANCE_URL") or "").rstrip("/")


def access_token() -> str:
    return (os.environ.get("SALESFORCE_ACCESS_TOKEN") or "").strip()


def is_connected() -> bool:
    return bool(instance_url() and access_token())


def tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    if context.get_ca_certs():
        return context
    try:
        import certifi
    except ImportError:
        return context
    return ssl.create_default_context(cafile=certifi.where())


def salesforce_request(method: str, url: str, payload=None) -> dict:
    headers = {
        "Authorization": f"Bearer {access_token()}",
        "Accept": "application/json",
        "User-Agent": "lead-hygiene-agent/1.0",
    }
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=45, context=tls_context()) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SalesforceApiError(f"Salesforce {method} {url} failed: {exc.code} {detail}") from exc
    return json.loads(raw) if raw else {}


def describe_lead() -> dict:
    """GET sobjects/Lead/describe. Requires SALESFORCE_INSTANCE_URL and token."""
    if not is_connected():
        raise SalesforceNotConnected(
            "Salesforce is not connected. Set SALESFORCE_INSTANCE_URL and "
            "SALESFORCE_ACCESS_TOKEN to refresh picklists from the org."
        )
    url = f"{instance_url()}/services/data/{API_VERSION}/sobjects/Lead/describe"
    return salesforce_request("GET", url)


def catalog_from_describe(describe: dict) -> dict:
    """Reduce a Lead describe payload to the picklist fields hygiene uses."""
    wanted = {name.lower(): name for name in PICKLIST_FIELD_NAMES}
    fields = {}
    for field in describe.get("fields") or []:
        api_name = field.get("name") or ""
        if api_name.lower() not in wanted:
            continue
        if field.get("type") not in {"picklist", "combobox"}:
            continue
        options = []
        for entry in field.get("picklistValues") or []:
            options.append({
                "value": entry.get("value") or "",
                "label": entry.get("label") or entry.get("value") or "",
                "active": bool(entry.get("active", True)),
            })
        fields[api_name] = {
            "restricted": bool(field.get("restrictedPicklist")),
            "controller": field.get("controllerName") or "",
            "options": options,
        }
    return {"source": "salesforce", "fields": fields}
