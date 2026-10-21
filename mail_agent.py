#!/usr/bin/env python3
"""Mail-Demo: Lieferanfrage per Mail -> KI fragt SAP -> Antwortentwurf in Outlook.

Prueft per IMAP die INBOX auf ungelesene Mails von DEMO_SENDER, laesst ein
LLM (Groq) ueber die lesenden SAP-Bausteine den Lieferstatus abfragen und legt
die Antwort als Entwurf (verknuepft mit der Anfrage) im Entwuerfe-Ordner ab.
Andere Mails werden nie gelesen oder als gelesen markiert.

Start: python mail_agent.py [--once] [--list-folders]
"""

import argparse
import base64
import email
import email.utils
import imaplib
import json
import os
import re
import sys
import time
from email.message import EmailMessage
from email.policy import default as default_policy
from pathlib import Path

import requests

from delivery_status_lookup import fetch_delivery_status
from sales_order_lookup import fetch_open_orders

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

SYSTEM_PROMPT = """Du bist Assistent im Vertriebsinnendienst. Lies die Kundenanfrage, \
ermittle Kunde und ggf. Material und frage den Status in SAP mit den Tools ab. \
Schreibe dann eine kurze, freundliche Antwortmail auf Deutsch (Sie-Form) mit \
Auftragsnummer, Material, Menge, Liefertermin (Format TT.MM.JJJJ) und Status.
Regeln:
- Keine erfundenen Daten, nur was die Tools liefern.
- Nichts gefunden: höflich nach der Auftragsnummer fragen.
- possibly_delayed=true ist nur ein Hinweis, keine Gewissheit: dann schreiben, \
dass wir den Termin intern prüfen und uns melden.
- Mehrere passende Aufträge: die neuesten (höchste Auftragsnummer), höchstens 3.
- SAP-Statuswerte ins Deutsche übersetzen (z.B. Initial = in Bearbeitung).
- Der Absender ist selbst der Kunde: direkt ansprechen ("Ihre Bestellung").
- Aufbau: Anrede mit Namen, ein Einleitungssatz, dann jeder Auftrag als Block \
mit einer Zeile je Angabe, dann ggf. Hinweis zum Termin, dann "Viele Grüße".
- Reiner Text, kein Markdown.
- Keine Gedankenstriche verwenden.
- Keine Signatur. Ende mit "Viele Grüße".
- Gib nur den Mailtext aus, ohne Betreff.
Kundennamen in SAP sind z.B. "bür-o-nline", "POLirado"; Materialnamen sind \
englisch, z.B. "Ring Binder - Green"."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_delivery_status",
            "description": "Lieferstatus je Auftragsposition eines Kunden, optional gefiltert nach Material.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {"type": "string", "description": "Kundenname (Teilstring)"},
                    "product_name": {"type": "string", "description": "Materialname (Teilstring, optional)"},
                },
                "required": ["customer_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_open_sales_orders",
            "description": "Offene Aufträge eines Kunden.",
            "parameters": {
                "type": "object",
                "properties": {"customer_name": {"type": "string", "description": "Kundenname (Teilstring)"}},
                "required": ["customer_name"],
            },
        },
    },
]


def load_env():
    env_file = Path(__file__).with_name(".env")
    if not env_file.exists():
        sys.exit(".env fehlt, siehe .env.example")
    cfg = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            cfg[key.strip()] = value.strip().strip('"').strip("'")
    cfg.setdefault("IMAP_PORT", "993")
    cfg.setdefault("IMAP_DRAFTS_FOLDER", "Entwürfe")
    cfg.setdefault("POLL_SECONDS", "10")
    missing = [k for k in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "DEMO_SENDER", "GROQ_API_KEY", "GROQ_MODEL") if not cfg.get(k)]
    if missing:
        sys.exit("Fehlende Werte in .env: " + ", ".join(missing))
    return cfg


def imap_utf7(name):
    """Ordnernamen in modified UTF-7 (IMAP) kodieren, z.B. 'Entwürfe'."""
    def encode(chunk):
        return "&" + base64.b64encode(chunk.encode("utf-16-be")).decode().rstrip("=").replace("/", ",") + "-"

    parts = re.split(r"([^\x20-\x7e]+)", name)
    out = "".join(encode(p) if i % 2 else p.replace("&", "&-") for i, p in enumerate(parts))
    return '"' + out + '"'


def connect(cfg):
    imap = imaplib.IMAP4_SSL(cfg["IMAP_HOST"], int(cfg["IMAP_PORT"]))
    imap.login(cfg["IMAP_USER"], cfg["IMAP_PASSWORD"])
    return imap


def run_tool(name, args):
    # DEMO_CUSTOMER fest gesetzt: das LLM kann keine fremden Kunden abfragen
    customer = (os.environ.get("DEMO_CUSTOMER") or args.get("customer_name") or "").strip()
    if not customer:
        return [], "Kundenname fehlt"
    if name == "get_delivery_status":
        rows = fetch_delivery_status(customer, args.get("product_name"))
        for row in rows:
            if row.get("DeliveryDate") is not None:
                row["DeliveryDate"] = row["DeliveryDate"].date().isoformat()
    elif name == "get_open_sales_orders":
        keys = ("SalesOrderID", "CustomerName", "GrossAmount", "CurrencyCode", "LifecycleStatusDescription")
        rows = [{k: o.get(k) for k in keys} for o in fetch_open_orders(customer)]
    else:
        return [], "Unbekanntes Tool"
    # neueste zuerst, gekürzt: hält Groq-Free-Tier-Tokenlimit ein
    rows.sort(key=lambda r: r.get("SalesOrderID") or "", reverse=True)
    return rows[:10], None


def body_text(msg):
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def ask_llm(cfg, sender, subject, text):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "Von: {0}\nBetreff: {1}\n\n{2}".format(sender, subject, text)},
    ]
    headers = {"Authorization": "Bearer " + cfg["GROQ_API_KEY"]}
    for _ in range(5):
        for _attempt in range(4):
            resp = requests.post(
                GROQ_URL,
                headers=headers,
                json={"model": cfg["GROQ_MODEL"], "messages": messages, "tools": TOOLS, "temperature": 0.2},
                timeout=60,
            )
            if resp.status_code != 429:
                break
            wait = min(float(resp.headers.get("retry-after", 10)), 30)
            print("  Groq-Limit erreicht, warte {0:.0f}s ...".format(wait))
            time.sleep(wait)
        resp.raise_for_status()
        reply = resp.json()["choices"][0]["message"]
        calls = reply.get("tool_calls") or []
        if not calls:
            # Gedankenstriche/geschützte Bindestriche hart ersetzen, LLM hält sich nicht immer daran
            text = re.sub(r" [–—] ", ", ", reply.get("content", "").strip())
            text = text.replace("–", "-").replace("—", "-").replace("‑", "-")
            return "\n".join(line.rstrip() for line in text.splitlines())
        messages.append(reply)
        for call in calls:
            name = call["function"]["name"]
            args = json.loads(call["function"].get("arguments") or "{}")
            rows, err = run_tool(name, args)
            print("  SAP-Abfrage: {0}({1}) -> {2} Treffer".format(name, args, len(rows)))
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps({"error": err} if err else rows, ensure_ascii=False, default=str),
            })
    raise RuntimeError("LLM hat nach 5 Runden keine Antwort geliefert")


def build_draft(cfg, original, answer, text):
    subject = original.get("Subject", "")
    if not re.match(r"^(AW|RE|Re|Aw):", subject):
        subject = "AW: " + subject
    quoted = "\n".join("> " + line for line in text.splitlines())
    draft = EmailMessage()
    draft["From"] = cfg["IMAP_USER"]
    draft["To"] = original.get("From")
    draft["Subject"] = subject
    draft["Date"] = email.utils.formatdate(localtime=True)
    if original.get("Message-ID"):
        draft["In-Reply-To"] = original["Message-ID"]
        draft["References"] = (original.get("References", "") + " " + original["Message-ID"]).strip()
    try:
        sent = email.utils.parsedate_to_datetime(original["Date"]).strftime("%d.%m.%Y um %H:%M")
    except (TypeError, ValueError):
        sent = original.get("Date", "")
    draft.set_content("{0}\n\nAm {1} schrieb {2}:\n{3}\n".format(
        answer, sent, original.get("From", ""), quoted))
    return draft


def process_once(imap, cfg):
    imap.select("INBOX")
    _, data = imap.uid("search", None, "UNSEEN", "FROM", '"{0}"'.format(cfg["DEMO_SENDER"]))
    for uid in data[0].split():
        try:
            _, msg_data = imap.uid("fetch", uid, "(BODY.PEEK[])")
            original = email.message_from_bytes(msg_data[0][1], policy=default_policy)
            print("Neue Anfrage von {0}: {1}".format(original.get("From"), original.get("Subject")))
            text = body_text(original)
            answer = ask_llm(cfg, original.get("From"), original.get("Subject"), text)
            draft = build_draft(cfg, original, answer, text)
            imap.append(imap_utf7(cfg["IMAP_DRAFTS_FOLDER"]), r"(\Draft)",
                        imaplib.Time2Internaldate(time.time()), draft.as_bytes())
            imap.uid("store", uid, "+FLAGS", r"(\Seen)")
            print("  Entwurf abgelegt in {0}".format(cfg["IMAP_DRAFTS_FOLDER"]))
        except Exception as exc:  # pro Mail abfangen, Schleife laeuft weiter
            print("  Fehler: {0}".format(exc))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="einen Durchlauf machen und beenden")
    parser.add_argument("--list-folders", action="store_true", help="IMAP-Ordner auflisten und beenden")
    opts = parser.parse_args()
    cfg = load_env()

    imap = connect(cfg)
    if opts.list_folders:
        for line in imap.list()[1]:
            print(line.decode(errors="replace"))
        return

    print("Mail-Agent läuft, wartet auf Mails von {0} ...".format(cfg["DEMO_SENDER"]))
    while True:
        try:
            process_once(imap, cfg)
        except (imaplib.IMAP4.abort, OSError) as exc:
            print("IMAP-Verbindung verloren ({0}), verbinde neu ...".format(exc))
            imap = connect(cfg)
            continue
        if opts.once:
            break
        time.sleep(int(cfg["POLL_SECONDS"]))
    imap.logout()


if __name__ == "__main__":
    main()
