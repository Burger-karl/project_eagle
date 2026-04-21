# Project Eagle — SAGE 200 → FIRS MBS Middleware

Production-ready middleware that integrates **SAGE 200** (via MSSQL/ODBC) with
the **FIRS NRS Merchant Buyer Solution** e-Invoice platform.

---

## Architecture

```
SAGE 200 MSSQL (.bak restored)
        ↓  ODBC (read-only)
Django Middleware (Project Eagle)
        ↓  Celery task queue
FIRS MBS API (via Access Point Provider)
        ↓  IRN + CSID returned
SAGE 200 REST API (write IRN back)
```

## Project Structure

```
project_eagle/
├── config/
│   ├── settings/
│   │   ├── base.py          ← All shared settings
│   │   ├── development.py   ← Local dev overrides
│   │   └── production.py    ← Production (security hardened)
│   ├── celery.py            ← Celery app + beat schedule
│   └── urls.py              ← Root URL routing
│
├── apps/
│   ├── core/
│   │   └── models.py        ← ClientCompany model (multi-tenant)
│   │
│   ├── sage200/
│   │   ├── odbc_client.py   ← READ invoices from MSSQL via ODBC
│   │   ├── api_client.py    ← OAuth2 auth + IRN write-back
│   │   ├── field_mapper.py  ← Map Sage 200 fields → FIRS UBL
│   │   └── irn_writer.py    ← Coordinates IRN write-back
│   │
│   ├── firs/
│   │   ├── client.py        ← FIRS MBS API client
│   │   └── invoice_schema.py ← Build 55-field UBL payload
│   │
│   └── invoices/
│       ├── models.py        ← Invoice, InvoiceLine, BuyerTIN, SubmissionLog
│       ├── pipeline.py      ← Full Sage→FIRS→writeback orchestration
│       ├── tasks.py         ← Celery async tasks
│       ├── views.py         ← REST API endpoints
│       ├── serializers.py   ← JSON serialization
│       └── urls.py          ← API URL routing
│
├── utils/
│   ├── currency.py          ← CBN exchange rate fetcher
│   └── exceptions.py        ← Custom exception handler
│
├── middleware/
│   └── request_logger.py    ← HTTP request logging
│
└── tests/
    └── test_invoices/
        └── test_pipeline.py ← Unit tests for mapper + UBL builder
```

---

## Quick Start

### Prerequisites
- Python 3.12+
- PostgreSQL 15+
- Redis 7+
- SQL Server ODBC Driver 17 (for Sage 200 MSSQL connection)
- SQL Server Management Studio (to restore client .bak file)

### 1 — Restore the Sage 200 .bak file
Open SSMS → right-click Databases → Restore Database → browse to .bak → OK.

### 2 — Install dependencies
```bash
pip install -r requirements.txt
```

### 3 — Configure environment
```bash
cp .env.example .env
# Edit .env with your credentials:
# - SAGE200_MSSQL_SERVER and SAGE200_MSSQL_DATABASE
# - FIRS_API_KEY and FIRS_SECRET_KEY
# - SAGE200_CLIENT_ID and SAGE200_CLIENT_SECRET (for IRN write-back)
```

### 4 — Run database migrations
```bash
python manage.py migrate
python manage.py createsuperuser
```

### 5 — Configure a client company
```bash
python manage.py shell
```
```python
from apps.core.models import ClientCompany
ClientCompany.objects.create(
    name="Client Company Ltd",
    tin="12345678-0001",
    address="Lagos, Nigeria",
    email="admin@client.com",
    mssql_server=r"DESKTOP-XXXX\SQLEXPRESS",
    mssql_database="your_sage200_db",
    firs_api_key="your_firs_api_key",
    firs_secret_key="your_firs_secret_key",
)
```

### 6 — Add buyer TIN mappings
These map Sage 200 customer account references to their FIRS TINs.
```python
from apps.invoices.models import BuyerTINMapping
from apps.core.models import ClientCompany

company = ClientCompany.objects.first()
BuyerTINMapping.objects.create(
    client=company,
    sage_account_ref="CUS001",
    buyer_name="Buyer Company Ltd",
    buyer_tin="98765432-0001",
)
```

### 7 — Start all services
```bash
# Terminal 1 — Django dev server
python manage.py runserver

# Terminal 2 — Celery worker (processes invoice tasks)
celery -A config worker --loglevel=info

# Terminal 3 — Celery Beat (triggers poll every 5 minutes)
celery -A config beat --loglevel=info
```

### 8 — Or use Docker Compose
```bash
docker-compose up --build
```

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/invoices/health/` | System health check |
| GET | `/api/invoices/stats/` | Dashboard statistics |
| POST | `/api/invoices/poll/` | Manually trigger Sage 200 poll |
| GET | `/api/invoices/` | List all invoices |
| GET | `/api/invoices/{id}/` | Invoice detail |
| POST | `/api/invoices/{id}/submit/` | Manually submit to FIRS |
| POST | `/api/invoices/{id}/retry/` | Retry a failed invoice |
| GET | `/api/invoices/validate-tin/?tin=XXX` | Validate buyer TIN |
| GET | `/api/invoices/buyer-tins/` | List TIN mappings |
| POST | `/api/invoices/buyer-tins/` | Add TIN mapping |
| GET | `/api/docs/` | Swagger UI |

---

## Invoice Status Flow

```
PENDING → SUBMITTING → CLEARED → WRITTEN_BACK
                   ↓
                REJECTED / FAILED → (retry) → SUBMITTING
```

---

## Running Tests

```bash
pytest tests/ -v
```

---

## Key Design Decisions

**ODBC for reading, REST API for writing**
We read Sage 200 invoices via ODBC (fast, no credentials needed beyond Windows Auth).
We write IRN/CSID back via the official Sage 200 REST API to respect business logic.

**Exponential back-off on failures**
Failed invoices retry after 5min → 10min → 20min → 40min → 1hr (max 5 attempts).

**Multi-tenant from day one**
Each ClientCompany has its own MSSQL connection string and FIRS credentials.
One middleware instance serves all of Link Options' clients.

**Full audit trail**
Every FIRS API call is logged in SubmissionLog — required for NITDA compliance.
