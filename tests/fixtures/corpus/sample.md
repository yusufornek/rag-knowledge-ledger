---
license: MIT
tenant: acme-support
---

# Sample Markdown Document

This document exercises the native Markdown parser: headings,
paragraphs, a list, a table, and a fenced code block.

## Background

The support desk test scripts sometimes reference a synthetic card
number such as 4111 1111 1111 1111 -- the well-known payment-sandbox
test Visa number, never a real card.

## Details

- First bullet about the refund policy
- Second bullet about escalation paths
- Third bullet about the synthetic IBAN TR330006100519786457841326 used
  only in tests

Table: Regional contacts

| Region | Contact |
|---|---|
| EU | eu-support@example.com |
| US | us-support@example.com |

```python
def escalate(ticket_id):
    print("escalating", ticket_id)
```
