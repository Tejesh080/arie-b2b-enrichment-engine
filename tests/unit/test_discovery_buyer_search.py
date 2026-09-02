import httpx
import pytest

from arie.config import HunterConfig
from arie.discovery.buyer_search import (
    BuyerSearchError,
    RawBuyerRecord,
    _email_status,
    _to_candidate,
    buyer_search_eligible,
    find_buyers,
    rank_buyers,
)
from arie.discovery.models import EmailStatus
from arie.recommendations import CustomerPriority


def _record(
    *,
    first_name: str | None = "Jordan",
    last_name: str | None = "Lee",
    position: str | None = "Operations Director",
    seniority_enum: str | None = "director",
    department_enum: str | None = "operations",
    decision_maker: bool | None = True,
    email: str | None = "jordan@example.com",
    email_confidence: float | None = 0.95,
    email_verification_status: str | None = "valid",
    linkedin: str | None = None,
) -> RawBuyerRecord:
    return RawBuyerRecord(
        first_name=first_name,
        last_name=last_name,
        position=position,
        seniority_enum=seniority_enum,
        department_enum=department_enum,
        decision_maker=decision_maker,
        email=email,
        email_confidence=email_confidence,
        email_verification_status=email_verification_status,
        linkedin=linkedin,
    )


# ------------------------------------------------------------------- gate --


def test_buyer_search_eligible_for_positive_priorities() -> None:
    assert buyer_search_eligible(priority=CustomerPriority.CONTACT_FIRST, existing_buyer_name=None)
    assert buyer_search_eligible(priority=CustomerPriority.WORTH_PURSUING, existing_buyer_name=None)


def test_buyer_search_ineligible_for_skip_and_review() -> None:
    assert not buyer_search_eligible(priority=CustomerPriority.SKIP, existing_buyer_name=None)
    assert not buyer_search_eligible(priority=CustomerPriority.REVIEW, existing_buyer_name=None)


def test_buyer_search_ineligible_when_buyer_already_known() -> None:
    assert not buyer_search_eligible(
        priority=CustomerPriority.CONTACT_FIRST, existing_buyer_name="Jordan Lee"
    )


# --------------------------------------------------------- record -> candidate --


def test_to_candidate_drops_nameless_records() -> None:
    record = _record(first_name=None, last_name=None)
    assert _to_candidate(record, source="hunter_domain_search") is None


def test_to_candidate_preserves_provider_identity() -> None:
    candidate = _to_candidate(_record(), source="hunter_domain_search")
    assert candidate is not None
    assert candidate.full_name == "Jordan Lee"
    assert candidate.title == "Operations Director"
    assert candidate.function == "operations"
    assert candidate.decision_maker is True
    assert candidate.source == "hunter_domain_search"


def test_to_candidate_never_invents_an_email() -> None:
    candidate = _to_candidate(_record(email=None, email_confidence=None), source="s")
    assert candidate is not None
    assert candidate.email is None
    assert candidate.email_status is EmailStatus.NONE


# ------------------------------------------------------------------ email --


def test_email_status_verified_from_provider_verification() -> None:
    assert _email_status(_record(email_verification_status="valid")) is EmailStatus.VERIFIED


def test_email_status_likely_from_high_confidence_without_verification() -> None:
    record = _record(email_verification_status=None, email_confidence=0.95)
    assert _email_status(record) is EmailStatus.LIKELY


def test_email_status_unverified_for_low_confidence() -> None:
    record = _record(email_verification_status=None, email_confidence=0.3)
    assert _email_status(record) is EmailStatus.UNVERIFIED


def test_email_status_none_when_no_email() -> None:
    record = _record(email=None, email_confidence=None, email_verification_status=None)
    assert _email_status(record) is EmailStatus.NONE


# ----------------------------------------------------------------- ranking --


def test_rank_buyers_prefers_matching_seniority_and_function() -> None:
    matching = _record(
        first_name="Match", last_name="Er", seniority_enum="director", department_enum="operations"
    )
    off_target = _record(
        first_name="Off", last_name="Target", seniority_enum="ic", department_enum="engineering"
    )
    ranked = rank_buyers(
        [off_target, matching],
        preferred_seniorities=("director", "c_level"),
        preferred_functions=("operations",),
    )
    assert ranked[0].full_name == "Match Er"


def test_rank_buyers_never_invents_a_candidate_not_in_the_input() -> None:
    """No LLM touches this ranking — it can only reorder what the provider
    returned, never add or fabricate an entry."""
    records = [_record(first_name="A"), _record(first_name="B", last_name="Lee")]
    ranked = rank_buyers(records, preferred_seniorities=(), preferred_functions=())
    assert {c.full_name for c in ranked} == {"A Lee", "B Lee"}
    assert len(ranked) == len(records)


def test_rank_buyers_prefers_decision_maker_and_usable_email() -> None:
    weak = _record(
        first_name="Weak",
        last_name="Signal",
        decision_maker=False,
        email=None,
        email_confidence=None,
        email_verification_status=None,
    )
    strong = _record(first_name="Strong", last_name="Signal", decision_maker=True)
    ranked = rank_buyers([weak, strong], preferred_seniorities=(), preferred_functions=())
    assert ranked[0].full_name == "Strong Signal"


# --------------------------------------------------------------- transport --


def test_find_buyers_refuses_when_unconfigured() -> None:
    with pytest.raises(BuyerSearchError):
        find_buyers("example.com", limit=5, config=HunterConfig(api_key=""))


def test_find_buyers_treats_404_as_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code = 404

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    records = find_buyers("example.com", limit=5, config=HunterConfig(api_key="k"))
    assert records == []


def test_find_buyers_parses_a_real_shaped_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code = 200

        def json(self) -> dict:  # type: ignore[type-arg]
            return {
                "data": {
                    "emails": [
                        {
                            "first_name": "Sarah",
                            "last_name": "Chen",
                            "position": "Operations Director",
                            "seniority": "director",
                            "department": "operations",
                            "decision_maker": True,
                            "value": "sarah@example.com",
                            "confidence": 95,
                            "verification": {"status": "valid"},
                            "linkedin": "https://linkedin.com/in/sarahchen",
                        }
                    ]
                }
            }

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    records = find_buyers("example.com", limit=5, config=HunterConfig(api_key="k"))
    assert len(records) == 1
    assert records[0].first_name == "Sarah"
    assert records[0].email == "sarah@example.com"
    assert records[0].email_confidence == pytest.approx(0.95)


def test_find_buyers_isolates_transport_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "get", _raise)
    with pytest.raises(BuyerSearchError):
        find_buyers("example.com", limit=5, config=HunterConfig(api_key="k"))
