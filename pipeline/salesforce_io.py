"""
salesforce_io.py
Load and save Lead rows through the Salesforce REST API.

This is the later half of the I/O seam. It is unused until
SALESFORCE_INSTANCE_URL and SALESFORCE_ACCESS_TOKEN are set.
The CSV those calls produce is the same shape process_csv already accepts.

System fields are never written back. Phone stays a string so the leading +
is not turned into a number.
"""

import csv
import io
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

API_VERSION = "v61.0"
# Match a typical Lead export. Extra fields can be set in SALESFORCE_LEAD_FIELDS.
DEFAULT_LEAD_FIELDS = (
    "Id", "FirstName", "LastName", "Email", "Phone", "MobilePhone",
    "Company", "Title", "Website", "Industry", "Country",
    "LeadSource", "Status", "AnnualRevenue", "NumberOfEmployees",
)
# Salesforce owns these. Hygiene must not PATCH them.
READ_ONLY_FIELDS = frozenset({
    "CreatedDate", "LastModifiedDate", "SystemModstamp", "LastActivityDate",
    "LastViewedDate", "LastReferencedDate",
})
MAX_COMPOSITE = 200


class SalesforceNotConnected(Exception):
    """Credentials are missing. Use a CSV export until they are set."""


class SalesforceApiError(Exception):
    """The Salesforce API rejected the call."""


def instance_url() -> str:
    return (os.environ.get("SALESFORCE_INSTANCE_URL") or "").rstrip("/")


def access_token() -> str:
    return (os.environ.get("SALESFORCE_ACCESS_TOKEN") or "").strip()


def is_connected() -> bool:
    return bool(instance_url() and access_token())


def lead_fields() -> list:
    raw = os.environ.get("SALESFORCE_LEAD_FIELDS") or ""
    if raw.strip():
        return [part.strip() for part in raw.split(",") if part.strip()]
    return list(DEFAULT_LEAD_FIELDS)


def require_connected() -> None:
    if is_connected():
        return
    raise SalesforceNotConnected(
        "Salesforce is not connected. Run against a CSV export for now. "
        "To switch later, set SALESFORCE_INSTANCE_URL and SALESFORCE_ACCESS_TOKEN "
        "and use --source salesforce. The hygiene job does not change."
    )


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


def query_url(soql: str) -> str:
    encoded = urllib.parse.quote(soql, safe="")
    return f"{instance_url()}/services/data/{API_VERSION}/query?q={encoded}"


def composite_url() -> str:
    return f"{instance_url()}/services/data/{API_VERSION}/composite/sobjects"


def records_to_csv(records: list, fields: list) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    for record in records:
        row = {}
        for field in fields:
            value = record.get(field)
            row[field] = "" if value is None else str(value)
        writer.writerow(row)
    return output.getvalue()


def load_leads_csv() -> str:
    """SOQL query → the same CSV text a Data Loader export would produce."""
    require_connected()
    fields = lead_fields()
    soql = "SELECT " + ", ".join(fields) + " FROM Lead"
    records = []
    url = query_url(soql)
    while url:
        data = salesforce_request("GET", url)
        records.extend(data.get("records") or [])
        if data.get("done") or not data.get("nextRecordsUrl"):
            break
        url = instance_url() + data["nextRecordsUrl"]
    return records_to_csv(records, fields)


def csv_to_update_records(csv_text: str) -> list:
    """Build Composite PATCH rows. Id is required; system fields are dropped."""
    rows = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        lead_id = (row.get("Id") or "").strip()
        if not lead_id:
            continue
        record = {"attributes": {"type": "Lead"}, "Id": lead_id}
        for field, value in row.items():
            if field in {"Id", ""} or field in READ_ONLY_FIELDS:
                continue
            if field.replace("_", "") == "":
                continue
            record[field] = value
        rows.append(record)
    return rows


def save_leads_csv(csv_text: str) -> None:
    """PATCH each surviving Lead. Phones stay strings, including a leading +."""
    require_connected()
    records = csv_to_update_records(csv_text)
    for start in range(0, len(records), MAX_COMPOSITE):
        batch = records[start:start + MAX_COMPOSITE]
        salesforce_request("PATCH", composite_url(), {"allOrNone": False, "records": batch})
