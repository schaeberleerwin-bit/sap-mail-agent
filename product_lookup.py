#!/usr/bin/env python3
"""Listet Materialien/Produkte nach Kategorie aus dem GWSAMPLE_BASIC OData-Service.

Verwendung:
    python product_lookup.py "Kategorie"

Beispiel:
    python product_lookup.py "Files & Binders"

Das Feld 'Category' ist im GWSAMPLE_BASIC-Service NICHT filterbar
(sap:filterable="false", genau wie 'Name'). Deshalb wird die komplette
Produktliste einmalig geladen und clientseitig case-insensitiv per Teilstring
auf 'Category' gefiltert. Mehrere Treffer sind gewuenscht; kein Treffer liefert
eine leere Liste (kein Fehler).
"""

import sys

import requests
from requests.auth import HTTPBasicAuth
import urllib3

from sap_config import PASSWORD, SERVICE_ROOT, USERNAME, VERIFY_TLS

# Self-signed TLS -> Zertifikatspruefung deaktiviert, entsprechende Warnung unterdruecken.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Service-Wurzel (ohne Entity-Set) - die einzelnen Collections werden angehaengt.
PRODUCT_SET_URL = SERVICE_ROOT + "/ProductSet"

# Reine Leseabfrage; 30s sind mehr als ausreichend fuer den kleinen ProductSet.
GET_TIMEOUT = 30


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


def _fetch_all_products(session):
    """Laedt die Produktliste einmalig vom Service (fuer clientseitiges Filtern).

    Das Feld 'Category' ist im GWSAMPLE_BASIC-Service NICHT filterbar
    (sap:filterable="false" -> serverseitiges substringof liefert HTTP 400
    "Filter not supported"). Der ProductSet ist mit ~205 Eintraegen klein
    genug, um die Liste einmal zu laden und clientseitig zu durchsuchen.

    Returns:
        Liste von Produkt-Dicts mit ProductID, Name, Category, MeasureUnit,
        Price und CurrencyCode.
    """
    response = session.get(
        PRODUCT_SET_URL,
        params={
            "$select": "ProductID,Name,Category,MeasureUnit,Price,CurrencyCode",
            "$format": "json",
        },
        headers={"Accept": "application/json"},
        verify=VERIFY_TLS,
        timeout=GET_TIMEOUT,
    )
    response.raise_for_status()
    return response.json().get("d", {}).get("results", [])


def fetch_products_by_category(category):
    """Listet alle Produkte, deren Kategorie den Suchbegriff (Teilstring) enthaelt.

    Das Feld 'Category' ist serverseitig nicht filterbar, deshalb wird die
    komplette Produktliste einmalig geladen und clientseitig case-insensitiv
    per Teilstring gegen 'Category' gefiltert (gleiches Muster wie
    _resolve_product in order_create.py, hier aber als Liste - mehrere Treffer
    sind erwuenscht).

    Args:
        category: Kategorie (case-insensitiver Teilstring, Suche in 'Category').

    Returns:
        Liste von Dicts mit ProductID, Name, Category, Price, CurrencyCode und
        MeasureUnit. Leere Liste, wenn kein Produkt passt (kein Fehler).

    Raises:
        requests.HTTPError: Bei HTTP-Fehlern der OData-Abfrage.
    """
    session = requests.Session()
    session.auth = HTTPBasicAuth(USERNAME, PASSWORD)

    all_products = _fetch_all_products(session)

    term = (category or "").lower()
    matches = [p for p in all_products if term in (p.get("Category") or "").lower()]

    return [
        {
            "ProductID": p.get("ProductID"),
            "Name": p.get("Name"),
            "Category": p.get("Category"),
            "Price": p.get("Price"),
            "CurrencyCode": p.get("CurrencyCode"),
            "MeasureUnit": p.get("MeasureUnit"),
        }
        for p in matches
    ]


def main():
    # Windows-Konsole nutzt standardmaessig cp1252 -> deutsche/franz. Umlaute
    # wuerden falsch dargestellt. stdout/stderr auf UTF-8 zwingen.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")

    args = sys.argv[1:]
    if not args:
        print(
            "Verwendung: python product_lookup.py \"Kategorie\"",
            file=sys.stderr,
        )
        return 1

    category = args[0]

    try:
        products = fetch_products_by_category(category)
    except requests.RequestException as exc:
        print(
            "Fehler beim Abrufen der Produkte: {0}".format(exc),
            file=sys.stderr,
        )
        return 1

    if not products:
        print("Keine Produkte gefunden, deren Kategorie '{0}' enthaelt.".format(category))
        return 0

    print(
        "{0} Produkt(e) in Kategorie '{1}':".format(len(products), category)
    )
    for product in products:
        print(
            "  {0} ({1}) | {2} {3} | {4}".format(
                product.get("Name", "?"),
                product.get("ProductID", "?"),
                product.get("Price", "?"),
                product.get("CurrencyCode", ""),
                product.get("MeasureUnit", ""),
            )
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
