"""Golden tests for incident signatures (app/signature.py): pure functions, no database.

The table below IS the specification of normalize_message. If a row has to change, the rule
changed: bump SIGNATURE_VERSION and rebuild incidents (scripts/rebuild_incidents.py).
"""
import pytest

import app.signature as signature
from app.signature import (
    SignatureParts,
    build_parts,
    compute_signature,
    normalize_message,
)

GOLDEN = [
    # --- the rows of the design record
    ('The credential "Example-marketing" needs to be reconnected.',
     'the credential "<q>" needs to be reconnected'),
    ("Request failed with status code 429", "request failed with status code 429"),
    ("Timeout of 30000ms exceeded", "timeout of <n>ms exceeded"),
    ("Sheet 1AbCdEf9876543210xyz not found", "sheet <id> not found"),
    ("Cannot read properties of undefined (reading 'email')",
     "cannot read properties of undefined (reading 'email')"),
    # --- values that change every run
    ("Execution 3f2b8c1e-9d4a-4c7e-8a1b-5e6f7a8b9c0d failed", "execution <uuid> failed"),
    ("Token expired at 2026-10-30T19:04:25.784Z", "token expired at <ts>"),
    ("Token expired at 2026-10-30 19:04:25+02:00", "token expired at <ts>"),
    ("No data for 2026-10-30", "no data for <ts>"),
    ("Invalid recipient jane.doe@example.com", "invalid recipient <email>"),
    ("commit deadbeefcafe1234 rejected", "commit <id> rejected"),
    ("Expired at 1730000000000", "expired at <id>"),
    ("Item 7 of 12 failed", "item 7 of <n> failed"),
    ("Retry after 120 seconds", "retry after <n> seconds"),
    # --- URLs: scheme and host stay, path and query go
    ("GET https://api.example.com/v2/orders/8841?page=3 failed",
     "get https://api.example.com/<path> failed"),
    ("Cannot reach https://api.example.com", "cannot reach https://api.example.com"),
    ("Cannot reach https://api.example.com/", "cannot reach https://api.example.com"),
    ("Cannot reach (https://api.example.com/a/b).", "cannot reach (https://api.example.com/<path>)"),
    ("See https://docs.example.com/errors/42.", "see https://docs.example.com/<path>"),
    ("Failed https://admin:hunter2@db.example.com/x", "failed https://db.example.com/<path>"),
    # --- 4xx/5xx status codes are kept; other numbers are not
    ("401 Unauthorized", "401 unauthorized"),
    ("Got 404 then 500", "got 404 then 500"),
    ("Expected 200 got 429", "expected <n> got 429"),
    ("took 429ms", "took <n>ms"),
    ("error 600 unknown", "error <n> unknown"),
    # --- quotes: double replaced, single kept
    ("Field \"email\" missing in 'items'", "field \"<q>\" missing in 'items'"),
    ("Unexpected token < in JSON at position 0", "unexpected token < in json at position 0"),
    # --- words that merely look long are untouched
    ("Authentication unsuccessful, internationalization", "authentication unsuccessful, internationalization"),
    # --- whitespace and trailing punctuation
    ("  Line one\n\n  line   two  ", "line one line two"),
    ("Something failed!!!", "something failed"),
    ("Something failed . ", "something failed"),
    # --- secrets are redacted BEFORE anything else, and never survive
    ("Request failed. Authorization: Bearer abc123def456ghi789", "request failed. authorization: [redacted]"),
    ('Login failed {"user":"bob","password":"hunter2hunter2"}',
     'login failed {"<q>":"<q>","<q>":[redacted]}'),
    ("GET https://api.example.com/x?api_key=sk-live-1234567890abcdef failed",
     "get https://api.example.com/<path> failed"),
    # --- documented crudeness: dotted IPs are just numbers
    ("connect ECONNREFUSED 10.0.0.12:5432", "connect econnrefused <n>.0.0.<n>:<n>"),
    # --- nothing to normalize
    ("", ""),
    (None, ""),
    ("   ", ""),
]


@pytest.mark.parametrize("raw, expected", GOLDEN, ids=[repr(r)[:50] for r, _ in GOLDEN])
def test_golden_normalization(raw, expected):
    assert normalize_message(raw) == expected


@pytest.mark.parametrize("raw, expected", GOLDEN, ids=[repr(r)[:50] for r, _ in GOLDEN])
def test_normalization_is_idempotent_and_deterministic(raw, expected):
    once = normalize_message(raw)
    assert normalize_message(raw) == once
    assert normalize_message(once) == once


def test_secrets_never_survive():
    for secret in ("abc123def456ghi789", "sk-live-1234567890abcdef", "hunter2hunter2"):
        for raw in (
            f"Authorization: Bearer {secret}",
            f"https://x.example.com/a?api_key={secret}",
            f'{{"password":"{secret}"}}',
            f"token={secret}",
        ):
            assert secret not in normalize_message(raw)


def test_message_is_capped_at_300_after_placeholders():
    long = normalize_message("word " * 200)
    assert 0 < len(long) <= signature.MESSAGE_CAP
    # A placeholder straddling the cut-off is dropped whole, never left as "<uui".
    text = ("x " * 148) + "3f2b8c1e-9d4a-4c7e-8a1b-5e6f7a8b9c0d tail"
    cut = normalize_message(text)
    assert cut.endswith("x") and "<" not in cut
    # ... and the cap is measured AFTER the placeholders shrink a long URL/id.
    shrunk = normalize_message(("x " * 100) + "https://api.example.com/" + "a/" * 100 + " end")
    assert shrunk.endswith("end")


# ------------------------------------------------------------------ signature inputs


def test_build_parts_reads_name_and_message_from_n8n_json():
    parts = build_parts(
        node_name=" SHEETS — Load Settings ",
        error_raw='{"name": "NodeApiError", "message": "Request failed with status code 429", "stack": "x"}',
        error_summary="NodeApiError: ignored because the JSON has a message",
    )
    assert parts == SignatureParts("SHEETS — Load Settings", "NodeApiError",
                                   "request failed with status code 429")


def test_build_parts_free_text_uses_the_sanitized_summary_and_has_no_error_name():
    parts = build_parts(node_name="HTTP", error_raw="Error: boom 12345", error_summary="Error: boom 12345")
    assert parts == SignatureParts("HTTP", "", "error: boom <n>")


@pytest.mark.parametrize("raw", [None, "", "not json", "[1, 2]", '"just a string"', "42", '{"name": "X"}',
                                 '{"message": 5}', '{"message": "  "}'])
def test_build_parts_falls_back_to_the_summary_when_the_json_has_no_message(raw):
    parts = build_parts(node_name=None, error_raw=raw, error_summary="Summary text 99")
    assert parts.normalized_message == "summary text <n>"


def test_build_parts_with_nothing_at_all():
    assert build_parts(node_name=None, error_raw=None, error_summary=None) == SignatureParts(None, "", "")


@pytest.mark.parametrize("node", [None, "", "   "])
def test_a_missing_node_is_unattributed(node):
    parts = build_parts(node_name=node, error_raw=None, error_summary="x")
    assert parts.node_name is None
    assert compute_signature(1, parts) == compute_signature(1, SignatureParts(None, "", "x"))


# ------------------------------------------------------------------ the signature itself


def test_signature_golden_value():
    """Pins the exact hash for v1. If this fails the rules (or the format) changed."""
    parts = SignatureParts("SHEETS — Load Settings", "NodeApiError", 'the credential "<q>" needs to be reconnected')
    assert signature.SIGNATURE_VERSION == "v1"
    # Checked independently: sha256 of the literal string
    # 'v1|4|SHEETS — Load Settings|NodeApiError|the credential "<q>" needs to be reconnected'.
    assert compute_signature(4, parts) == "ccc9ba66e1ca8b54"


def test_signature_shape_and_stability():
    parts = SignatureParts("N", "E", "m")
    sig = compute_signature(1, parts)
    assert len(sig) == 16 and int(sig, 16) >= 0
    assert compute_signature(1, parts) == sig


@pytest.mark.parametrize("other", [
    (2, SignatureParts("N", "E", "m")),       # another workflow
    (1, SignatureParts("N2", "E", "m")),      # another node
    (1, SignatureParts(None, "E", "m")),      # unattributed
    (1, SignatureParts("N", "E2", "m")),      # another error name
    (1, SignatureParts("N", "", "m")),        # no error name
    (1, SignatureParts("N", "E", "m2")),      # another message
])
def test_every_part_changes_the_signature(other):
    assert compute_signature(*other) != compute_signature(1, SignatureParts("N", "E", "m"))


def test_a_pipe_in_a_field_cannot_move_a_boundary():
    a = compute_signature(1, SignatureParts("a|b", "c", "m"))
    b = compute_signature(1, SignatureParts("a", "b|c", "m"))
    assert a != b
    assert compute_signature(1, SignatureParts("a%7Cb", "c", "m")) != a  # the escape itself is escaped


def test_the_version_is_part_of_the_signature(monkeypatch):
    parts = SignatureParts("N", "E", "m")
    v1 = compute_signature(1, parts)
    monkeypatch.setattr(signature, "SIGNATURE_VERSION", "v2")
    assert compute_signature(1, parts) != v1
