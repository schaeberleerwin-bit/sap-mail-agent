#!/usr/bin/env python3
"""MCP-Server, der offene SAP Sales Orders als Tool exponiert.

Wiederverwendung der bestehenden Logik aus sales_order_lookup.py
(fetch_open_orders) - es wird keine Abfragelogik dupliziert.

Transport: stdio (Standard fuer lokale MCP-Clients wie Claude Desktop).

Rollen (Env SAP_ROLE): 'vertrieb' (Lesen + Anlegen, Default) oder
'viewer' (nur Lesen). Andere/unbekannte Rollen duerfen nur lesen.
"""

import os
from typing import Optional

import requests
from mcp.server.fastmcp import FastMCP

# Bestehende Logik wiederverwenden - nichts duplizieren.
from sales_order_lookup import DEFAULT_CUSTOMER, fetch_open_orders
from delivery_status_lookup import fetch_delivery_status
from order_create import create_sales_order as _create_sales_order
from product_lookup import fetch_products_by_category

# Rolle aus der Umgebung lesen (fail closed: unbekannte Rollen -> nur Lesen).
ROLE = os.environ.get("SAP_ROLE", "vertrieb").strip().lower()
WRITE_ROLES = {"vertrieb"}

mcp = FastMCP("sap-mail-agent")


@mcp.tool()
def get_open_sales_orders(customer_name: Optional[str] = DEFAULT_CUSTOMER) -> list[dict]:
    """Liefert die offenen Sales Orders fuer einen Kunden als strukturierte Daten.

    Args:
        customer_name: Kundenname (Teilstring-Suche). Optional,
            Standard: 'bür-o-nline'.

    Returns:
        Liste von Auftraegen mit den Feldern SalesOrderID, CustomerName,
        GrossAmount, CurrencyCode und LifecycleStatusDescription.
    """
    if not customer_name:
        customer_name = DEFAULT_CUSTOMER

    try:
        orders = fetch_open_orders(customer_name)
    except requests.RequestException as exc:
        # Fehler als aussagekraeftige MCP-Fehlermeldung weiterreichen.
        raise RuntimeError("Abfrage der Sales Orders fehlgeschlagen: {0}".format(exc))

    return [
        {
            "SalesOrderID": order.get("SalesOrderID"),
            "CustomerName": order.get("CustomerName"),
            "GrossAmount": order.get("GrossAmount"),
            "CurrencyCode": order.get("CurrencyCode"),
            "LifecycleStatusDescription": order.get("LifecycleStatusDescription"),
        }
        for order in orders
    ]


@mcp.tool()
def get_delivery_status(
    customer_name: Optional[str] = DEFAULT_CUSTOMER,
    product_name: Optional[str] = None,
) -> list[dict]:
    """Liefert den Material-/Lieferstatus je Auftragsposition eines Kunden.

    HEURISTIK-EINSCHRAENKUNG: Das SAP-Demo-System (GWSAMPLE_BASIC) bietet
    keinen eigenen Delivery-/Logistics-Service mit echten Versand-Trackingdaten
    - nur Auftragsdaten sind verfuegbar. Das Feld 'possibly_delayed' ist daher
    eine HEURISTIK (True, wenn der Liefertermin in der Vergangenheit liegt und
    der Auftrag weder 'Completed'/'Closed' noch 'Delivered' ist) und keine
    bestaetigte Lieferverzoegerung. Bitte diese Einschraenkung beim Antworten
    transparent machen.

    Args:
        customer_name: Kundenname (Teilstring-Suche). Optional,
            Standard: 'bür-o-nline'.
        product_name: Optionaler Materialname (case-insensitiver Teilstring)
            zum Filtern der Positionen. Standard: None (kein Filter).

    Returns:
        Liste von Positionen mit den Feldern SalesOrderID,
        DeliveryStatusDescription, LifecycleStatusDescription, ProductID,
        ProductName, DeliveryDate (ISO-String oder None), Quantity,
        QuantityUnit und possibly_delayed.
    """
    if not customer_name:
        customer_name = DEFAULT_CUSTOMER

    try:
        entries = fetch_delivery_status(customer_name, product_name)
    except requests.RequestException as exc:
        # Fehler als aussagekraeftige MCP-Fehlermeldung weiterreichen.
        raise RuntimeError("Abfrage des Lieferstatus fehlgeschlagen: {0}".format(exc))

    result = []
    for entry in entries:
        delivery_date = entry.get("DeliveryDate")
        result.append(
            {
                "SalesOrderID": entry.get("SalesOrderID"),
                "DeliveryStatusDescription": entry.get("DeliveryStatusDescription"),
                "LifecycleStatusDescription": entry.get("LifecycleStatusDescription"),
                "ProductID": entry.get("ProductID"),
                "ProductName": entry.get("ProductName"),
                "DeliveryDate": delivery_date.isoformat() if delivery_date is not None else None,
                "Quantity": entry.get("Quantity"),
                "QuantityUnit": entry.get("QuantityUnit"),
                "possibly_delayed": entry.get("possibly_delayed"),
            }
        )
    return result


@mcp.tool()
def create_sales_order(
    customer_name: str,
    items: list[dict],
    note: Optional[str] = None,
) -> dict:
    """Legt einen ECHTEN Sales Order im SAP-Demo-System an (SCHREIBENDER Aufruf).

    ACHTUNG - KEIN DRY-RUN: Dieses Tool legt tatsaechlich einen neuen Auftrag
    inkl. Positionen im a4h-Demo-System (GWSAMPLE_BASIC) an. Der Aufruf ist
    schreibend und veraendert den Systemzustand (neue SalesOrderID, verbrauchte
    Nummernkreisnummer). Bitte nur ausfuehren, wenn ein Auftrag wirklich
    angelegt werden soll, und dies dem Nutzer gegenueber transparent machen.

    Der Kundenname wird als Teilstring gegen CompanyName aufgeloest (Kundenrolle
    '01' / Sold-To). Jeder Produktname in 'items' wird case-insensitiv als
    Teilstring gegen das Feld 'Name' im ProductSet aufgeloest; ProductID und
    QuantityUnit (aus dem Produktstammsatz-Feld MeasureUnit) werden daraus
    ermittelt. Nicht gefundene oder mehrdeutige Produktnamen fuehren zu einem
    Fehler (keine Rateloesung).

    Args:
        customer_name: Kundenname (Teilstring-Suche in CompanyName).
        items: Liste von Positionen, je ein Dict mit den Schluesseln
            'product_name' (str) und 'quantity' (float/int).
        note: Optionale Kopf-Notiz zum Auftrag. Standard: None.

    Returns:
        Dict mit 'SalesOrderID' (die neu vergebene Auftragsnummer) und 'items'
        (Liste der angelegten Positionen mit ProductID, ProductName, Quantity,
        QuantityUnit).

    Erfordert Rolle 'vertrieb' (Env SAP_ROLE); andere Rollen werden
    abgelehnt.
    """
    if ROLE not in WRITE_ROLES:
        raise PermissionError(
            "Keine Berechtigung: Die Rolle '{0}' darf keine Aufträge anlegen "
            "(nur Lesezugriff). Bitte an einen Nutzer mit Rolle 'vertrieb' "
            "wenden.".format(ROLE)
        )
    try:
        result = _create_sales_order(customer_name, items, note=note)
    except ValueError as exc:
        # Fachliche Fehler (Kunde/Produkt nicht aufloesbar) als klare Meldung.
        raise RuntimeError("Auftrag konnte nicht angelegt werden: {0}".format(exc))
    except requests.RequestException as exc:
        # HTTP-/SAP-Fehler (inkl. extrahierter SAP-Fehlermeldung) weiterreichen.
        raise RuntimeError("Anlegen des Auftrags fehlgeschlagen: {0}".format(exc))

    return {
        "SalesOrderID": result.get("SalesOrderID"),
        "items": [
            {
                "ProductID": item.get("ProductID"),
                "ProductName": item.get("ProductName"),
                "Quantity": item.get("Quantity"),
                "QuantityUnit": item.get("QuantityUnit"),
            }
            for item in result.get("items", [])
        ],
    }


@mcp.tool()
def list_products_by_category(category: str) -> list[dict]:
    """Listet Materialien/Produkte einer Kategorie aus dem GWSAMPLE_BASIC-Service.

    EINSCHRAENKUNG: Das Feld 'Category' ist im GWSAMPLE_BASIC-Service NICHT
    filterbar (sap:filterable="false"), ein serverseitiges $filter/substringof
    scheitert mit HTTP 400. Deshalb wird die komplette Produktliste einmalig
    geladen und CLIENT-SEITIG case-insensitiv per Teilstring gegen 'Category'
    gefiltert. Mehrere Treffer sind erwuenscht; passt kein Produkt, wird eine
    leere Liste zurueckgegeben (kein Fehler).

    Args:
        category: Kategorie (case-insensitiver Teilstring, Suche in 'Category').

    Returns:
        Liste von Produkten mit den Feldern ProductID, Name, Category, Price,
        CurrencyCode und MeasureUnit. Leere Liste, wenn nichts passt.
    """
    try:
        products = fetch_products_by_category(category)
    except requests.RequestException as exc:
        # Fehler als aussagekraeftige MCP-Fehlermeldung weiterreichen.
        raise RuntimeError("Abfrage der Produkte fehlgeschlagen: {0}".format(exc))

    return [
        {
            "ProductID": product.get("ProductID"),
            "Name": product.get("Name"),
            "Category": product.get("Category"),
            "Price": product.get("Price"),
            "CurrencyCode": product.get("CurrencyCode"),
            "MeasureUnit": product.get("MeasureUnit"),
        }
        for product in products
    ]


def main():
    """Startet den MCP-Server ueber stdio-Transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
