"""Golden table for the PII scrub (app/sanitize.py::scrub_pii) that guards the LLM context."""
import pytest

from app.sanitize import scrub, scrub_pii

GOLDEN = [
    # --- e-mail
    ("Invalid recipient jane.doe@example.com", "Invalid recipient <email>"),
    ("cc: A.B+tag@Sub.Example.CO.UK, x@y.io", "cc: <email>, <email>"),
    # --- URLs: userinfo and query go, host and path stay
    ("GET https://api.example.com/v2/orders?page=3&email=a@b.com failed",
     "GET https://api.example.com/v2/orders?<query> failed"),
    ("Failed https://admin:hunter2@db.example.com/x", "Failed https://db.example.com/x"),
    ("https://api.example.com/a?x=1#frag", "https://api.example.com/a?<query>"),
    ("https://api.example.com/plain/path", "https://api.example.com/plain/path"),
    # --- phone numbers
    ("Call +359 88 123 4567 now", "Call <phone> now"),
    ("Phone (02) 981-2345 busy", "Phone <phone> busy"),
    ("tel 0888123456 invalid", "tel <number> invalid"),
    ("fax +1.415.555.0132", "fax <phone>"),
    # --- cards, IBANs, long digit runs
    ("Card 4111 1111 1111 1111 declined", "Card <number> declined"),
    ("Card 4111-1111-1111-1111 declined", "Card <number> declined"),
    ("IBAN BG80BNBG96611020345678 invalid", "IBAN <number> invalid"),
    ("IBAN DE89 3704 0044 0532 0130 00 invalid", "IBAN <number> invalid"),
    ("Customer 1234567890 not found", "Customer <number> not found"),
    ("expired at 1730000000000", "expired at <number>"),
    # --- NOT touched: timestamps, short ids, line numbers, versions, status codes
    ("Token expired at 2026-10-30T19:04:25.784Z", "Token expired at 2026-10-30T19:04:25.784Z"),
    ("Token expired at 2026-10-30 19:04:25+02:00", "Token expired at 2026-10-30 19:04:25+02:00"),
    ("No data for 2026-10-30", "No data for 2026-10-30"),
    ("at ExecuteContext.apiRequest (index.ts:80:9)", "at ExecuteContext.apiRequest (index.ts:80:9)"),
    ("execution 20628 failed with status code 429", "execution 20628 failed with status code 429"),
    ("n8n 1.112.3 on node 22.11.0", "n8n 1.112.3 on node 22.11.0"),
    ("connect ECONNREFUSED 10.0.0.12:5432", "connect ECONNREFUSED 10.0.0.12:5432"),
    # --- nothing to do
    ("", ""),
    ("The credential \"Example-marketing\" needs to be reconnected.",
     "The credential \"Example-marketing\" needs to be reconnected."),
]


@pytest.mark.parametrize("raw, expected", GOLDEN, ids=[repr(r)[:55] for r, _ in GOLDEN])
def test_golden(raw, expected):
    assert scrub_pii(raw) == expected


@pytest.mark.parametrize("raw, expected", GOLDEN, ids=[repr(r)[:55] for r, _ in GOLDEN])
def test_idempotent(raw, expected):
    assert scrub_pii(expected) == expected


def test_scrub_removes_secrets_first_and_then_pii():
    out = scrub("Authorization: Bearer abc123def456ghi789 for jane@example.com, card 4111 1111 1111 1111")
    assert "abc123def456ghi789" not in out and "jane@" not in out and "4111" not in out
    assert "[REDACTED]" in out and "<email>" in out and "<number>" in out


def test_a_timestamp_inside_a_sentence_is_kept():
    assert scrub_pii("at 2026-10-30 19:04 by user") == "at 2026-10-30 19:04 by user"
