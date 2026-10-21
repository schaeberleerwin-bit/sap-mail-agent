#!/usr/bin/env python3
"""Legt einen Sales Order im SAP GWSAMPLE_BASIC OData-Service an (Deep Insert).

Verwendung:
    python order_create.py [Kundenname] "Produktname:Menge" ["Produktname:Menge" ...] [--note "..."]

Beispiel:
    python order_create.py "bür-o-nline" "Notebook Basic 15:2" "Notebook Basic 17:1" --note "Eilauftrag"

Ohne Kundenname wird 'bür-o-nline' verwendet. Es MUSS mindestens eine Position
im Format 'Produktname:Menge' angegeben werden. Der Produktname wird
case-insensitiv als Teilstring gegen das Feld 'Name' im ProductSet aufgeloest.

ACHTUNG: Dies ist ein SCHREIBENDER Aufruf. Es wird ein echter Auftrag im
a4h-Demo-System angelegt (kein Dry-Run).
"""

import sys

import requests
from requests.auth import HTTPBasicAuth
import urllib3

from sap_config import PASSWORD, SERVICE_ROOT, USERNAME, VERIFY_TLS

# Self-signed TLS -> Zertifikatspruefung deaktiviert, entsprechende Warnung unterdruecken.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Service-Wurzel (ohne Entity-Set) - die einzelnen Collections werden angehaengt.
SALES_ORDER_SET_URL = SERVICE_ROOT + "/SalesOrderSet"
BUSINESS_PARTNER_SET_URL = SERVICE_ROOT + "/BusinessPartnerSet"
PRODUCT_SET_URL = SERVICE_ROOT + "/ProductSet"
DEFAULT_CUSTOMER = "bür-o-nline"

# Der POST auf SalesOrderSet dauert im a4h-Container wegen der Nummernkreis-
# Vergabe deutlich laenger als eine Leseabfrage; 30s reichten in Tests nicht.
POST_TIMEOUT = 90
GET_TIMEOUT = 30

# Business-Partner-Rolle des Kunden (Sold-To). Im GWSAMPLE_BASIC-Stammsatz kann
# derselbe Name mehrfach vorkommen (z.B. Rolle 01 = Kunde, 02 = Lieferant);
# fuer einen Sales Order wird die Kundenrolle '01' benoetigt.
CUSTOMER_ROLE = "01"

# Fallback-Waehrung, falls am Kunden keine CurrencyCode hinterlegt ist.
DEFAULT_CURRENCY = "EUR"

# Liefertermin fuer neue Positionen ist im Metadatenmodell ein Pflichtfeld
# (Nullable="false"). SAP OData V2 erwartet Datumsangaben als /Date(ms)/.
# Wir setzen einen Termin ca. 7 Tage in der Zukunft.
_DELIVERY_OFFSET_DAYS = 7


def _sap_date_in_days(days):
    """Liefert einen SAP-OData-V2-Datumsstring '/Date(ms)/' fuer 'days' in der Zukunft."""
    from datetime import datetime, timedelta, timezone

    target = datetime.now(tz=timezone.utc) + timedelta(days=days)
    # Auf Mitternacht UTC normalisieren (DeliveryDate ist ein reines Datum).
    target = target.replace(hour=0, minute=0, second=0, microsecond=0)
    millis = int(target.timestamp() * 1000)
    return "/Date({0})/".format(millis)


def _extract_sap_error(response):
    """Zieht die SAP-OData-Fehlermeldung aus dem Response-Body (JSON oder Text).

    SAP liefert Fehler i.d.R. als {"error": {"message": {"value": "..."}}}.
    Gibt einen lesbaren String zurueck (leer, wenn nichts extrahierbar).
    """
    try:
        body = response.json()
    except ValueError:
        return (response.text or "").strip()
    error = body.get("error", {}) if isinstance(body, dict) else {}
    message = error.get("message", {})
    if isinstance(message, dict):
        return (message.get("value") or "").strip()
    return str(message).strip()


def _resolve_customer(session, customer_name):
    """Loest einen Kundennamen (Teilstring) zu CustomerID + CurrencyCode auf.

    Sucht im BusinessPartnerSet nach CompanyName und waehlt die Kundenrolle
    '01' (Sold-To). Wirft ValueError, wenn kein passender Kunde gefunden wird.

    Returns:
        Tupel (customer_id, currency_code). currency_code kann None sein.
    """
    params = {
        "$filter": "substringof('{0}',CompanyName)".format(
            customer_name.replace("'", "''")
        ),
        "$format": "json",
    }
    response = session.get(
        BUSINESS_PARTNER_SET_URL,
        params=params,
        headers={"Accept": "application/json"},
        verify=VERIFY_TLS,
        timeout=GET_TIMEOUT,
    )
    response.raise_for_status()
    partners = response.json().get("d", {}).get("results", [])

    if not partners:
        raise ValueError(
            "Kein Business Partner mit Namen '{0}' gefunden.".format(customer_name)
        )

    # Bevorzugt die Kundenrolle '01'; sonst den ersten Treffer nehmen.
    customers = [p for p in partners if p.get("BusinessPartnerRole") == CUSTOMER_ROLE]
    if not customers:
        # Kein Sold-To gefunden - der Name existiert nur in anderer Rolle.
        raise ValueError(
            "Business Partner '{0}' existiert, aber nicht in Kundenrolle "
            "'{1}' (Sold-To). Gefundene Rollen: {2}".format(
                customer_name,
                CUSTOMER_ROLE,
                ", ".join(sorted({p.get("BusinessPartnerRole") for p in partners})),
            )
        )

    partner = customers[0]
    return partner.get("BusinessPartnerID"), partner.get("CurrencyCode")


def _fetch_all_products(session):
    """Laedt die Produktliste (ProductID, Name, MeasureUnit) einmalig vom Service.

    Das Feld 'Name' ist im GWSAMPLE_BASIC-Service NICHT filterbar
    (sap:filterable="false" -> serverseitiges substringof liefert HTTP 400
    "Filter not supported"). Der ProductSet ist mit ~205 Eintraegen klein
    genug, um die Liste einmal zu laden und clientseitig zu durchsuchen.

    Returns:
        Liste von Produkt-Dicts mit ProductID, Name und MeasureUnit.
    """
    response = session.get(
        PRODUCT_SET_URL,
        params={"$select": "ProductID,Name,MeasureUnit", "$format": "json"},
        headers={"Accept": "application/json"},
        verify=VERIFY_TLS,
        timeout=GET_TIMEOUT,
    )
    response.raise_for_status()
    return response.json().get("d", {}).get("results", [])


def _resolve_product(product_name, all_products):
    """Loest einen Produktnamen (Teilstring, case-insensitiv) zu einem Produkt auf.

    Sucht clientseitig im Feld 'Name' der bereits geladenen Produktliste
    (siehe _fetch_all_products; 'Name' ist serverseitig nicht filterbar).
    Wirft ValueError, wenn der Name nicht gefunden wird oder nicht eindeutig
    ist (mehrere Treffer).

    Returns:
        Dict mit mindestens ProductID, Name und MeasureUnit.
    """
    term = product_name.lower()
    matches = [p for p in all_products if term in (p.get("Name") or "").lower()]

    if not matches:
        raise ValueError(
            "Kein Produkt gefunden, dessen Name '{0}' enthaelt.".format(product_name)
        )

    if len(matches) > 1:
        names = ", ".join(sorted({p.get("Name") for p in matches})[:10])
        raise ValueError(
            "Produktname '{0}' ist nicht eindeutig ({1} Treffer): {2}".format(
                product_name, len(matches), names
            )
        )

    return matches[0]


def _fetch_csrf_token(session):
    """Fuehrt den CSRF-Token-Handshake durch und liefert das Token zurueck.

    Ein GET auf die SalesOrderSet-Collection mit Header 'X-CSRF-Token: Fetch'
    liefert das Token im Response-Header; die Session-Cookies werden dabei in
    der uebergebenen Session mitgefuehrt und spaeter beim POST wiederverwendet.
    """
    response = session.get(
        SALES_ORDER_SET_URL,
        params={"$top": "1", "$format": "json"},
        headers={"X-CSRF-Token": "Fetch", "Accept": "application/json"},
        verify=VERIFY_TLS,
        timeout=GET_TIMEOUT,
    )
    response.raise_for_status()
    token = response.headers.get("x-csrf-token") or response.headers.get("X-CSRF-Token")
    if not token:
        raise RuntimeError(
            "CSRF-Token-Handshake fehlgeschlagen: kein Token im Response-Header."
        )
    return token


def create_sales_order(customer_name, items, note=None):
    """Legt per Deep Insert einen Sales Order inkl. Positionen im SAP-System an.

    Args:
        customer_name: Kundenname (Teilstring-Suche in CompanyName). Es wird die
            Kundenrolle '01' (Sold-To) verwendet, um die CustomerID aufzuloesen.
        items: Liste von Dicts {"product_name": str, "quantity": float}. Jeder
            product_name wird case-insensitiv als Teilstring gegen ProductSet
            (Feld 'Name') aufgeloest; daraus werden ProductID und QuantityUnit
            (aus dem Produktstammsatz-Feld MeasureUnit) ermittelt.
        note: Optionale Kopf-Notiz zum Auftrag.

    Returns:
        Dict mit 'SalesOrderID' und 'items' (Liste der angelegten Positionen mit
        ProductID, ProductName, Quantity, QuantityUnit).

    Raises:
        ValueError: Wenn Kunde oder ein Produktname nicht (eindeutig) aufloesbar
            ist oder keine Positionen uebergeben wurden.
        requests.HTTPError: Bei HTTP-Fehlern; die SAP-Fehlermeldung aus dem
            JSON-Body wird der Exception vorangestellt.
    """
    if not items:
        raise ValueError("Es muss mindestens eine Position (items) angegeben werden.")

    # Eine gemeinsame Session fuer CSRF-Fetch und POST (Cookies + Token gehoeren zusammen).
    session = requests.Session()
    session.auth = HTTPBasicAuth(USERNAME, PASSWORD)

    # 1) Kunde aufloesen.
    customer_id, customer_currency = _resolve_customer(session, customer_name)
    currency_code = customer_currency or DEFAULT_CURRENCY

    # 2) Positionen aufloesen (ProductID + QuantityUnit aus MeasureUnit).
    # Produktliste einmalig laden (Name ist serverseitig nicht filterbar).
    all_products = _fetch_all_products(session)
    delivery_date = _sap_date_in_days(_DELIVERY_OFFSET_DAYS)
    line_items = []
    resolved_items = []
    for index, entry in enumerate(items):
        product_name = entry.get("product_name")
        quantity = entry.get("quantity")
        if not product_name:
            raise ValueError("Jede Position benoetigt einen 'product_name'.")
        if quantity is None:
            raise ValueError(
                "Position '{0}' benoetigt eine 'quantity'.".format(product_name)
            )

        product = _resolve_product(product_name, all_products)
        product_id = product.get("ProductID")
        quantity_unit = product.get("MeasureUnit")
        product_display_name = product.get("Name")

        # SAP OData V2 erwartet Decimal-Werte als String.
        quantity_str = "{0}".format(quantity)

        # ItemPosition (zweiter Schluesselteil) MUSS beim Deep Insert mitgegeben
        # werden - obwohl $metadata sap:creatable auf dieser Property nicht setzt,
        # scheitert der POST sonst mit "Sales Order Line Item with key ... does
        # not exist" (leere Position). Positionen in 10er-Schritten nummerieren.
        item_position = "{0:010d}".format((index + 1) * 10)

        line_items.append(
            {
                "ItemPosition": item_position,
                "ProductID": product_id,
                "Quantity": quantity_str,
                "QuantityUnit": quantity_unit,
                "DeliveryDate": delivery_date,
            }
        )
        resolved_items.append(
            {
                "ProductID": product_id,
                "ProductName": product_display_name,
                "Quantity": quantity,
                "QuantityUnit": quantity_unit,
            }
        )

    # 3) CSRF-Token holen (gleiche Session).
    token = _fetch_csrf_token(session)

    # 4) Deep-Insert-Body zusammenbauen. Die Navigation-Property zu den
    # Positionen heisst laut $metadata 'ToLineItems'; SAP OData V2 erwartet
    # das verschachtelte Array unter dem Schluessel 'results'.
    payload = {
        "CustomerID": customer_id,
        "CurrencyCode": currency_code,
        "ToLineItems": {"results": line_items},
    }
    if note is not None:
        payload["Note"] = note

    # 5) Deep-Insert-POST.
    response = session.post(
        SALES_ORDER_SET_URL,
        json=payload,
        headers={
            "X-CSRF-Token": token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        verify=VERIFY_TLS,
        timeout=POST_TIMEOUT,
    )

    if response.status_code != 201:
        # SAP-Fehlertext extrahieren, damit ersichtlich ist, WARUM es fehlschlug.
        sap_error = _extract_sap_error(response)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            if sap_error:
                raise requests.HTTPError(
                    "{0} | SAP-Fehler: {1}".format(exc, sap_error),
                    response=response,
                )
            raise
        # Falls Status < 400 aber nicht 201 (unerwartet): sauber melden.
        raise RuntimeError(
            "Unerwarteter Status {0} beim Anlegen des Auftrags: {1}".format(
                response.status_code, sap_error or response.text
            )
        )

    # 6) Erfolg: SalesOrderID und angelegte Positionen aus der Response lesen.
    created = response.json().get("d", {})
    sales_order_id = created.get("SalesOrderID")

    # Positionen aus der Response uebernehmen, falls vorhanden; sonst die
    # aufgeloesten Eingabepositionen zurueckgeben.
    response_items = created.get("ToLineItems", {})
    if isinstance(response_items, dict):
        response_items = response_items.get("results", [])

    if response_items:
        items_out = [
            {
                "ProductID": it.get("ProductID"),
                # ProductName ist nicht Teil des LineItem-Response; aus Aufloesung uebernehmen.
                "ProductName": next(
                    (
                        r["ProductName"]
                        for r in resolved_items
                        if r["ProductID"] == it.get("ProductID")
                    ),
                    None,
                ),
                "Quantity": it.get("Quantity"),
                "QuantityUnit": it.get("QuantityUnit"),
            }
            for it in response_items
        ]
    else:
        items_out = resolved_items

    return {"SalesOrderID": sales_order_id, "items": items_out}


def _parse_item_arg(arg):
    """Parst ein CLI-Argument 'Produktname:Menge' zu {'product_name', 'quantity'}.

    Der Produktname darf Doppelpunkte enthalten; nur der LETZTE Doppelpunkt
    trennt die Menge ab.
    """
    if ":" not in arg:
        raise ValueError(
            "Position '{0}' hat nicht das Format 'Produktname:Menge'.".format(arg)
        )
    product_name, _, quantity_raw = arg.rpartition(":")
    product_name = product_name.strip()
    quantity_raw = quantity_raw.strip()
    if not product_name:
        raise ValueError("Position '{0}': leerer Produktname.".format(arg))
    try:
        quantity = float(quantity_raw)
    except ValueError:
        raise ValueError(
            "Position '{0}': Menge '{1}' ist keine Zahl.".format(arg, quantity_raw)
        )
    return {"product_name": product_name, "quantity": quantity}


def main():
    # Windows-Konsole nutzt standardmaessig cp1252 -> deutsche/franz. Umlaute
    # (z.B. in 'bür-o-nline') wuerden falsch dargestellt. stdout/stderr auf UTF-8 zwingen.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")

    # --note "..." aus den Argumenten herausloesen.
    args = sys.argv[1:]
    note = None
    if "--note" in args:
        idx = args.index("--note")
        if idx + 1 >= len(args):
            print("Fehler: --note erwartet einen Text.", file=sys.stderr)
            return 1
        note = args[idx + 1]
        args = args[:idx] + args[idx + 2:]

    if not args:
        print(
            "Verwendung: python order_create.py [Kundenname] "
            "\"Produktname:Menge\" [...] [--note \"...\"]",
            file=sys.stderr,
        )
        return 1

    # Enthaelt das erste Argument keinen ':', ist es der Kundenname; die
    # restlichen Argumente sind Positionen. Andernfalls sind alle Argumente
    # Positionen und der Default-Kunde wird verwendet.
    if ":" not in args[0]:
        customer_name = args[0]
        item_args = args[1:]
    else:
        customer_name = DEFAULT_CUSTOMER
        item_args = args

    if not item_args:
        print(
            "Fehler: mindestens eine Position 'Produktname:Menge' angeben.",
            file=sys.stderr,
        )
        return 1

    try:
        items = [_parse_item_arg(a) for a in item_args]
    except ValueError as exc:
        print("Fehler: {0}".format(exc), file=sys.stderr)
        return 1

    try:
        result = create_sales_order(customer_name, items, note=note)
    except ValueError as exc:
        print("Fehler: {0}".format(exc), file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print("Fehler beim Anlegen des Auftrags: {0}".format(exc), file=sys.stderr)
        return 1

    print(
        "Auftrag angelegt: SalesOrderID {0} (Kunde '{1}')".format(
            result.get("SalesOrderID", "?"), customer_name
        )
    )
    for item in result.get("items", []):
        print(
            "  {0} ({1}) | Menge: {2} {3}".format(
                item.get("ProductName", "?"),
                item.get("ProductID", "?"),
                item.get("Quantity", "?"),
                item.get("QuantityUnit", ""),
            )
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
