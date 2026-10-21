#!/usr/bin/env python3
"""Fragt Material-/Lieferstatus aus dem SAP GWSAMPLE_BASIC OData-Service ab.

Verwendung:
    python delivery_status_lookup.py [Kundenname] [Materialname]

Ohne Argument wird 'bür-o-nline' als Kundenname verwendet. Der Materialname
ist optional und filtert die Positionen als case-insensitiver Teilstring.
"""

import re
import sys
from datetime import datetime, timezone

import requests
from requests.auth import HTTPBasicAuth
import urllib3

from sap_config import PASSWORD, SERVICE_ROOT, USERNAME, VERIFY_TLS

# Self-signed TLS -> Zertifikatspruefung deaktiviert, entsprechende Warnung unterdruecken.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = SERVICE_ROOT + "/SalesOrderSet"
DEFAULT_CUSTOMER = "bür-o-nline"

# SAP OData V2 serialisiert Datumsangaben als "/Date(1234567890000)/"
# (Millisekunden seit Epoch, optional mit Zeitzonen-Offset "+0060").
_SAP_DATE_RE = re.compile(r"/Date\((-?\d+)([+-]\d+)?\)/")


def _parse_sap_date(raw):
    """Wandelt das SAP OData-Format /Date(ms)/ in ein datetime-Objekt um.

    Gibt None zurueck, wenn kein gueltiger Wert vorliegt.
    """
    if not raw:
        return None
    match = _SAP_DATE_RE.match(str(raw))
    if not match:
        return None
    millis = int(match.group(1))
    # UTC-basiertes datetime; Offset (falls vorhanden) ignorieren wir bewusst,
    # da SAP das Datum bereits als UTC-Epoch liefert.
    return datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc)


def fetch_delivery_status(customer_name, product_name=None):
    """Ruft Auftragspositionen inkl. Liefer-/Materialstatus fuer einen Kunden ab.

    HEURISTIK-EINSCHRAENKUNG:
        Dieses SAP-Demo-System (GWSAMPLE_BASIC) stellt KEINEN eigenen
        Delivery-/Logistics-Service mit echten Versand-Trackingdaten bereit.
        Es sind nur die Auftragsdaten (SalesOrderSet inkl. Positionen und
        Produkten) verfuegbar. Das berechnete Feld 'possibly_delayed' ist
        daher eine HEURISTIK als Ersatz fuer echtes Versand-Tracking:
        Es ist True, wenn das DeliveryDate in der Vergangenheit liegt UND der
        LifecycleStatusDescription nicht 'Completed'/'Closed' ist UND der
        DeliveryStatusDescription nicht 'Delivered' ist. Es handelt sich um
        eine Annaeherung, nicht um eine bestaetigte Lieferverzoegerung.

    Args:
        customer_name: Kundenname (Teilstring-Suche in CustomerName).
        product_name: Optionaler Materialname; filtert Positionen als
            case-insensitiver Teilstring auf ProductName.

    Returns:
        Liste von Dicts, ein Eintrag pro Auftragsposition, mit den Feldern:
        SalesOrderID, DeliveryStatusDescription, LifecycleStatusDescription,
        ProductID, ProductName, DeliveryDate (datetime oder None), Quantity,
        QuantityUnit und possibly_delayed (bool).
    """
    params = {
        "$filter": "substringof('{0}',CustomerName)".format(
            customer_name.replace("'", "''")
        ),
        "$expand": "ToLineItems/ToProduct",
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

    orders = response.json().get("d", {}).get("results", [])

    now = datetime.now(tz=timezone.utc)
    filter_term = product_name.lower() if product_name else None

    entries = []
    for order in orders:
        sales_order_id = order.get("SalesOrderID")
        lifecycle_status = order.get("LifecycleStatusDescription")

        line_items = order.get("ToLineItems", {})
        if isinstance(line_items, dict):
            line_items = line_items.get("results", [])

        for item in line_items:
            product = item.get("ToProduct") or {}
            if isinstance(product, dict) and "results" in product:
                # Bei ungewoehnlichen Serialisierungen defensiv bleiben.
                product = product.get("results") or {}
                if isinstance(product, list):
                    product = product[0] if product else {}

            product_name_value = product.get("Name")

            if filter_term is not None:
                if not product_name_value or filter_term not in product_name_value.lower():
                    continue

            # DeliveryStatusDescription existiert laut SAP-Metadaten NUR auf der
            # SalesOrder-Kopfebene (order), nicht auf der Positionsebene (item).
            # Daher von 'order' lesen, nicht von 'item' (dort immer None).
            delivery_status = order.get("DeliveryStatusDescription")
            delivery_date = _parse_sap_date(item.get("DeliveryDate"))

            possibly_delayed = bool(
                delivery_date is not None
                and delivery_date < now
                and lifecycle_status not in ("Completed", "Closed")
                and delivery_status != "Delivered"
            )

            entries.append(
                {
                    "SalesOrderID": sales_order_id,
                    "DeliveryStatusDescription": delivery_status,
                    "LifecycleStatusDescription": lifecycle_status,
                    "ProductID": item.get("ProductID") or product.get("ProductID"),
                    "ProductName": product_name_value,
                    "DeliveryDate": delivery_date,
                    "Quantity": item.get("Quantity"),
                    "QuantityUnit": item.get("QuantityUnit"),
                    "possibly_delayed": possibly_delayed,
                }
            )

    return entries


def main():
    # Windows-Konsole nutzt standardmaessig cp1252 -> deutsche/franz. Umlaute
    # (z.B. in 'bür-o-nline') wuerden falsch dargestellt. stdout/stderr auf UTF-8 zwingen.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")

    customer_name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CUSTOMER
    product_name = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        entries = fetch_delivery_status(customer_name, product_name)
    except requests.RequestException as exc:
        print("Fehler bei der Abfrage: {0}".format(exc), file=sys.stderr)
        return 1

    header = "Lieferstatus fuer Kunde '{0}'".format(customer_name)
    if product_name:
        header += " / Material '{0}'".format(product_name)
    print("{0}: {1} Position(en)".format(header, len(entries)))

    for entry in entries:
        delivery_date = entry.get("DeliveryDate")
        delivery_date_str = (
            delivery_date.date().isoformat() if delivery_date is not None else "?"
        )
        line = (
            "  {0} | {1} ({2}) | Menge: {3} {4} | Liefertermin: {5} | "
            "Lieferstatus: {6} | Lifecycle: {7}".format(
                entry.get("SalesOrderID", "?"),
                entry.get("ProductName", "?"),
                entry.get("ProductID", "?"),
                entry.get("Quantity", "?"),
                entry.get("QuantityUnit", ""),
                delivery_date_str,
                entry.get("DeliveryStatusDescription", "?"),
                entry.get("LifecycleStatusDescription", "?"),
            )
        )
        print(line)
        if entry.get("possibly_delayed"):
            print(
                "      [!] moeglicherweise verzoegert (Heuristik, kein echtes "
                "Versand-Tracking im Demo-System verfuegbar)"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
