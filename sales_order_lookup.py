#!/usr/bin/env python3
"""Fragt offene Sales Orders aus dem SAP GWSAMPLE_BASIC OData-Service ab.

Verwendung:
    python sales_order_lookup.py [Kundenname]

Ohne Argument wird 'bür-o-nline' als Kundenname verwendet.
"""

import sys

import requests
from requests.auth import HTTPBasicAuth
import urllib3

from sap_config import PASSWORD, SERVICE_ROOT, USERNAME, VERIFY_TLS

# Self-signed TLS -> Zertifikatspruefung deaktiviert, entsprechende Warnung unterdruecken.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = SERVICE_ROOT + "/SalesOrderSet"
DEFAULT_CUSTOMER = "bür-o-nline"


def fetch_open_orders(customer_name):
    """Ruft Auftraege fuer einen Kundennamen ab und liefert die offenen zurueck."""
    params = {
        "$filter": "substringof('{0}',CustomerName)".format(
            customer_name.replace("'", "''")
        ),
        "$format": "json",
    }
    response = requests.get(
        BASE_URL,
        params=params,
        auth=HTTPBasicAuth(USERNAME, PASSWORD),
        headers={"Accept": "application/json"},
        verify=VERIFY_TLS,
        timeout=30,
    )
    response.raise_for_status()

    results = response.json().get("d", {}).get("results", [])
    return [
        order
        for order in results
        if order.get("LifecycleStatusDescription") != "Closed"
    ]


def main():
    # Windows-Konsole nutzt standardmaessig cp1252 -> deutsche/franz. Umlaute
    # (z.B. in 'bür-o-nline') wuerden falsch dargestellt. stdout/stderr auf UTF-8 zwingen.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")

    customer_name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CUSTOMER

    try:
        open_orders = fetch_open_orders(customer_name)
    except requests.RequestException as exc:
        print("Fehler bei der Abfrage: {0}".format(exc), file=sys.stderr)
        return 1

    print("Offene Auftraege fuer Kunde '{0}': {1}".format(customer_name, len(open_orders)))
    for order in open_orders:
        print(
            "  {0} | {1} | {2} {3} | Status: {4}".format(
                order.get("SalesOrderID", "?"),
                order.get("CustomerName", "?"),
                order.get("GrossAmount", "?"),
                order.get("CurrencyCode", ""),
                order.get("LifecycleStatusDescription", "?"),
            )
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
