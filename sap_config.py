"""SAP-Verbindung aus Umgebungsvariablen bzw. .env (nie im Code)."""
import os

_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_ENV):
    with open(_ENV, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# z. B. https://<host>:<port>/sap/opu/odata/iwbep/GWSAMPLE_BASIC
SERVICE_ROOT = os.environ.get("SAP_SERVICE_ROOT", "").rstrip("/")
USERNAME = os.environ.get("SAP_USER", "")
PASSWORD = os.environ.get("SAP_PASSWORD", "")
# Nur für lokale Testsysteme mit selbstsigniertem Zertifikat auf false setzen
VERIFY_TLS = os.environ.get("SAP_VERIFY_TLS", "true").lower() != "false"
