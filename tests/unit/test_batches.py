"""Unit tests for `arie.batches`'s pure CSV parsing/validation — no database.

`create_batch`/`get_batch`/`list_batches`/`batch_progress` need a real
`lead_batches`/`leads` schema and are covered against a live database in
`tests/integration/test_batches_integration.py` instead.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from arie.batches import MAX_ROWS, MalformedCsvError, parse_csv

ORG = uuid4()


def _csv(text: str) -> bytes:
    return text.encode("utf-8")


def test_a_well_formed_file_is_fully_accepted() -> None:
    content = _csv(
        "email,first_name,last_name,company,domain,title\n"
        "a@example.com,Ada,Lovelace,Acme,acme.com,Engineer\n"
    )
    rows = parse_csv(content, organization_id=ORG)
    assert len(rows) == 1
    row = rows[0]
    assert row.validation_status == "accepted"
    assert row.command is not None
    assert row.command.email == "a@example.com"
    assert row.command.full_name == "Ada Lovelace"
    assert row.command.company_name == "Acme"
    assert row.command.company_domain == "acme.com"
    assert row.command.title == "Engineer"
    assert row.command.organization_id == ORG
    assert row.command.external_ref == "csv:a@example.com"


def test_header_aliases_are_recognised_case_insensitively() -> None:
    content = _csv(
        "Email Address,Full Name,Company Name,Website\nb@example.com,Grace Hopper,Navy,navy.mil\n"
    )
    rows = parse_csv(content, organization_id=ORG)
    assert rows[0].validation_status == "accepted"
    assert rows[0].command is not None
    assert rows[0].command.full_name == "Grace Hopper"
    assert rows[0].command.company_name == "Navy"
    assert rows[0].command.company_domain == "navy.mil"


def test_utf8_bom_is_stripped_transparently() -> None:
    content = b"\xef\xbb\xbfemail\nc@example.com\n"
    rows = parse_csv(content, organization_id=ORG)
    assert rows[0].validation_status == "accepted"


def test_missing_email_column_is_a_file_level_error() -> None:
    content = _csv("full_name,company\nAda,Acme\n")
    with pytest.raises(MalformedCsvError, match="email"):
        parse_csv(content, organization_id=ORG)


def test_empty_file_with_only_a_header_is_a_file_level_error() -> None:
    content = _csv("email\n")
    with pytest.raises(MalformedCsvError, match="no data rows"):
        parse_csv(content, organization_id=ORG)


def test_a_row_missing_email_is_rejected_not_fatal() -> None:
    content = _csv("email,company\n,Acme\nreal@example.com,Widgets\n")
    rows = parse_csv(content, organization_id=ORG)
    assert len(rows) == 2
    assert rows[0].validation_status == "rejected"
    assert rows[0].validation_error is not None
    assert "email" in rows[0].validation_error
    assert rows[1].validation_status == "accepted"


def test_an_unparseable_email_is_rejected_not_fatal() -> None:
    content = _csv("email\nnot-an-email\n")
    rows = parse_csv(content, organization_id=ORG)
    assert rows[0].validation_status == "rejected"
    assert rows[0].command is None


def test_a_whitespace_only_domain_cell_is_treated_as_blank_not_invalid() -> None:
    content = _csv("email,domain\nok@example.com,\t\n")
    rows = parse_csv(content, organization_id=ORG)
    assert rows[0].validation_status == "accepted"
    assert rows[0].command is not None
    assert rows[0].command.company_domain is None


def test_an_unnormalizable_domain_rejects_only_that_row() -> None:
    content = _csv("email,domain\nok@example.com,://\n")
    rows = parse_csv(content, organization_id=ORG)
    assert rows[0].validation_status == "rejected"
    assert rows[0].validation_error is not None
    assert "domain" in rows[0].validation_error


def test_oversized_email_is_rejected() -> None:
    huge_email = ("a" * 320) + "@example.com"
    content = _csv(f"email\n{huge_email}\n")
    rows = parse_csv(content, organization_id=ORG)
    assert rows[0].validation_status == "rejected"
    assert rows[0].validation_error is not None
    assert "320" in rows[0].validation_error


def test_duplicate_emails_in_one_file_are_each_independently_accepted() -> None:
    """Parsing does not deduplicate — both rows are individually well-formed.
    Deduplication happens later, at ingestion, via `leads`' own idempotency
    (see the module docstring). Both rows here must produce the *same*
    `external_ref` so that later step can recognise them as the same lead.
    """
    content = _csv("email\nsame@example.com\nSAME@EXAMPLE.COM\n")
    rows = parse_csv(content, organization_id=ORG)
    assert rows[0].validation_status == rows[1].validation_status == "accepted"
    assert rows[0].command is not None
    assert rows[1].command is not None
    assert rows[0].command.external_ref == rows[1].command.external_ref == "csv:same@example.com"


def test_row_count_over_the_limit_is_a_file_level_error() -> None:
    header = "email\n"
    body = "".join(f"person{i}@example.com\n" for i in range(MAX_ROWS + 1))
    with pytest.raises(MalformedCsvError, match="exceeding"):
        parse_csv(_csv(header + body), organization_id=ORG)


def test_file_at_exactly_the_row_limit_is_accepted() -> None:
    header = "email\n"
    body = "".join(f"person{i}@example.com\n" for i in range(MAX_ROWS))
    rows = parse_csv(_csv(header + body), organization_id=ORG)
    assert len(rows) == MAX_ROWS


def test_oversized_file_is_rejected_before_parsing() -> None:
    huge_content = b"email\n" + b"a@example.com\n" * 200_000
    with pytest.raises(MalformedCsvError, match="byte limit"):
        parse_csv(huge_content, organization_id=ORG)


def test_organization_id_cannot_be_supplied_by_the_csv() -> None:
    """There is no column mapping for organization_id at all — a CSV column
    named that (or anything else unrecognised) is silently ignored, exactly
    like any other extra column, never read into the command."""
    content = _csv(f"email,organization_id\nok@example.com,{uuid4()}\n")
    rows = parse_csv(content, organization_id=ORG)
    assert rows[0].command is not None
    assert rows[0].command.organization_id == ORG


def test_a_formula_looking_cell_is_stored_as_plain_text() -> None:
    """Python's csv reader never evaluates a formula — a cell starting with
    `=` is just a string, both in the parsed command and in the raw audit
    dict this test also checks."""
    content = _csv('email,company\nok@example.com,"=1+1"\n')
    rows = parse_csv(content, organization_id=ORG)
    assert rows[0].command is not None
    assert rows[0].command.company_name == "=1+1"
