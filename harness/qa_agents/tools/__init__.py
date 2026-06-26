"""SDK tool definitions for the QA persona harness.

- ``email`` — send_email / wait_for_email / get_email (real SMTP + Mailpit).
- ``findings`` — note_finding plus the run-scoped Findings collector.
- ``identity`` — generate_identity (locale-aware fake persona identity).
- ``openapi`` — list_endpoints / get_endpoint / search over the tenant's
  OpenAPI spec (api-poker surface discovery).
"""
