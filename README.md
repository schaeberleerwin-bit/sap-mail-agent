# sap-mail-agent

KI-Agent, der Kundenanfragen per Mail beantwortet und dafür Daten aus SAP liest.

```
Kundenmail (IMAP) -> LLM mit Tool-Calling -> SAP OData (nur lesend) -> Antwortentwurf in Outlook
```

Der Agent versendet nichts selbst. Die Antwort liegt als Entwurf im Postfach, ein Mensch prüft und schickt ab.
Zusätzlich stellt ein MCP-Server dieselben SAP-Abfragen als Tools für Claude Desktop & Co. bereit.

Getestet gegen das SAP-Demo-Service `GWSAMPLE_BASIC` (SAP ABAP Platform Trial). Keine Kundendaten.

## Konfiguration

`.env.example` nach `.env` kopieren und ausfüllen. Zugangsdaten stehen nur dort, nie im Code.
`.env` ist per `.gitignore` ausgeschlossen.

## Tool

| Tool-Name              | Parameter                                   | Rückgabe |
|------------------------|---------------------------------------------|----------|
| `get_open_sales_orders`| `customer_name` (string, optional, Default `bür-o-nline`) | Liste offener Aufträge mit `SalesOrderID`, `CustomerName`, `GrossAmount`, `CurrencyCode`, `LifecycleStatusDescription` |
| `get_delivery_status`  | `customer_name` (string, optional, Default `bür-o-nline`), `product_name` (string, optional, Default `None`) | Liste der Auftragspositionen mit `SalesOrderID`, `DeliveryStatusDescription`, `LifecycleStatusDescription`, `ProductID`, `ProductName`, `DeliveryDate` (ISO-String), `Quantity`, `QuantityUnit`, `possibly_delayed` |
| `create_sales_order`   | `customer_name` (string), `items` (Liste von `{ "product_name": string, "quantity": number }`), `note` (string, optional) | Dict mit `SalesOrderID` (neu vergebene Auftragsnummer) und `items` (angelegte Positionen mit `ProductID`, `ProductName`, `Quantity`, `QuantityUnit`). **SCHREIBEND** – legt einen echten Auftrag im Demo-System an (kein Dry-Run). |
| `list_products_by_category` | `category` (string) | Liste von Produkten mit `ProductID`, `Name`, `Category`, `Price`, `CurrencyCode`, `MeasureUnit`. Client-seitig gefiltert (siehe Hinweis unten). |

> **Hinweis zur `possibly_delayed`-Heuristik:** Das SAP-Demo-System
> `GWSAMPLE_BASIC` stellt keinen eigenen Delivery-/Logistics-Service mit echten
> Versand-Trackingdaten bereit – es sind nur Auftragsdaten verfügbar. Das Feld
> `possibly_delayed` ist deshalb eine **Heuristik** (True, wenn der Liefertermin
> in der Vergangenheit liegt und der Auftrag weder `Completed`/`Closed` noch
> `Delivered` ist) und **keine bestätigte Lieferverzögerung**.

## Installation

```powershell
pip install -r requirements.txt
```

## Server lokal starten

Der Server nutzt den **stdio**-Transport. Direkt starten:

```powershell
python mcp_server.py
```

Der Prozess wartet dann auf einen MCP-Client, der über stdin/stdout kommuniziert.
Für ein interaktives Testen kann der MCP Inspector verwendet werden:

```powershell
npx @modelcontextprotocol/inspector python mcp_server.py
```

## Registrierung in Claude Desktop

Claude Desktop liest seine Konfiguration aus `claude_desktop_config.json`.
Unter Windows liegt die Datei typischerweise hier:

```
%APPDATA%\Claude\claude_desktop_config.json
```

Füge dort folgenden Eintrag hinzu (bzw. ergänze den `mcpServers`-Block):

```json
{
  "mcpServers": {
    "sap-mail-agent": {
      "command": "python",
      "args": ["<Pfad zum Repo>\\mcp_server.py"]
    }
  }
}
```

Hinweise:

- Falls `python` nicht im PATH von Claude Desktop liegt, den absoluten Pfad zur
  Python-Executable angeben, z. B.
  `"command": "C:\\Users\\<Benutzer>\\AppData\\Local\\Programs\\Python\\Python311\\python.exe"`.
- Nach dem Speichern Claude Desktop neu starten. Das Tool
  `get_open_sales_orders` erscheint anschließend in der Tool-Liste.

## Verwendung als klassisches Skript

Das ursprüngliche Skript funktioniert weiterhin eigenständig:

```powershell
python sales_order_lookup.py "bür-o-nline"
```

Auch das Anlegen eines Auftrags ist als Standalone-Skript möglich. Format der
Positionen: `Produktname:Menge` (mehrere Positionen als weitere Argumente), eine
Kopf-Notiz optional per `--note`:

```powershell
python order_create.py "bür-o-nline" "Ring Binder - Green:2" "Lever Arch File - Blue:1" --note "Eilauftrag"
```

> **Hinweis:** `order_create.py` / das Tool `create_sales_order` sind
> **schreibend** und legen einen echten Auftrag im a4h-Demo-System an (kein
> Dry-Run). Der Produktname wird case-insensitiv als Teilstring gegen das Feld
> `Name` im `ProductSet` aufgelöst; nicht eindeutige oder unbekannte Namen führen
> zu einem aussagekräftigen Fehler statt zu einer geratenen Position.

Produkte einer Kategorie lassen sich ebenfalls als Standalone-Skript auflisten.
Die Kategorie wird als case-insensitiver Teilstring interpretiert:

```powershell
python product_lookup.py "Files & Binders"
```

Das Skript gibt alle Treffer lesbar aus (`ProductID`, `Name`, `Price` +
`CurrencyCode`, `MeasureUnit`). Wird kein Produkt gefunden, erscheint eine
entsprechende Meldung und das Skript endet ohne Fehler.

> **Hinweis zur client-seitigen Filterung:** Das Feld `Category` ist im
> `GWSAMPLE_BASIC`-Service **nicht filterbar** (`sap:filterable="false"`, genau
> wie `Name`) – ein serverseitiges `$filter`/`substringof` scheitert mit
> HTTP 400. Deshalb lädt `product_lookup.py` / das Tool
> `list_products_by_category` die komplette Produktliste (der `ProductSet` ist
> mit ~205 Einträgen klein genug) einmalig und filtert **client-seitig**
> case-insensitiv per Teilstring auf `Category`. Mehrere Treffer sind hier
> erwünscht; kein Treffer liefert eine leere Liste (kein Fehler).

## Mail-Demo (Outlook)

Lieferanfrage per Mail, KI fragt SAP ab, Antwort liegt als Entwurf in Outlook.

1. `.env.example` nach `.env` kopieren und ausfüllen (Ordnername prüfen: `python mail_agent.py --list-folders`).
2. `python mail_agent.py` starten (Testlauf: `--once`).
3. Von `DEMO_SENDER` eine Anfrage schicken, z. B. „Wo bleibt unsere Bestellung Ring Binder? Gruß, bür-o-nline“.
4. In Outlook unter Entwürfe liegt nach wenigen Sekunden die Antwort, verknüpft mit der Anfrage. Prüfen, senden.

Nur Mails von `DEMO_SENDER` werden gelesen, alle anderen bleiben unberührt. Nur lesende SAP-Abfragen.

## Autor und Lizenz

Entwickelt von Erwin Schäberle, [Schäberle Digital](https://www.schaeberle-digital.de): KI-Anbindung an SAP und andere ERP-Systeme über MCP-Server.

Lizenz: MIT, siehe [LICENSE](LICENSE).
