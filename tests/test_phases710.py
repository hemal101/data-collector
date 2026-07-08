"""Tests for Phases 7-10 pure logic (no network).

Run:  .venv/bin/python tests/test_phases710.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import ai_enrich, contacts, score, verify


# --- Phase 7: AI enrichment (heuristic) ------------------------------------

def test_ai_business_type_b2c():
    c = {"name": "Trendy Threads", "industry": "Fashion", "sector": "Apparel",
         "description": "Online store to shop the latest fashion apparel."}
    e = ai_enrich.heuristic_enrich(c)
    assert e["business_type"] == "B2C"
    assert e["icp"] == "Consumers"

def test_ai_business_type_b2b():
    c = {"name": "DataForge", "industry": "Enterprise Software", "sector": "Analytics",
         "description": "An enterprise SaaS platform with an API for analytics."}
    e = ai_enrich.heuristic_enrich(c)
    assert e["business_type"] == "B2B"
    assert e["industry"] == "Enterprise Software"
    assert e["sub_industry"] == "Analytics"

def test_ai_keywords_and_description():
    c = {"name": "PredictML", "industry": "AI", "sector": "NLP",
         "description": "PredictML builds machine learning models for healthcare."}
    e = ai_enrich.heuristic_enrich(c)
    assert "AI" in e["keywords"] and "NLP" in e["keywords"]
    assert e["short_description"]
    assert e["model"] == "heuristic"

def test_ai_synthesizes_description_when_missing():
    c = {"name": "Acme Robotics", "industry": "Robotics", "city": "Pune", "stage": "EarlyTraction"}
    e = ai_enrich.heuristic_enrich(c)
    assert "Acme Robotics" in e["short_description"]
    assert "Pune" in e["short_description"]


# --- Phase 8: contact classification ---------------------------------------

def test_classify_role_emails():
    assert contacts.classify_email("support@acme.com")[0] == "support"
    assert contacts.classify_email("sales@acme.com")[0] == "sales"
    assert contacts.classify_email("careers@acme.com")[0] == "hr"
    assert contacts.classify_email("info@acme.com")[0] == "info"
    assert contacts.classify_email("contact@acme.com")[0] == "contact"

def test_classify_person_name():
    ctype, conf, name = contacts.classify_email("jane.doe@acme.com")
    assert name == "Jane Doe"
    assert 0 < conf <= 1

def test_build_contacts_adds_patterns_and_lowers_offdomain():
    found = [{"email": "founder@gmail.com", "source_page": "contact"}]
    out = contacts.build_contacts("acme.com", found)
    emails = {c["email"]: c for c in out}
    assert "info@acme.com" in emails and emails["info@acme.com"]["source"] == "pattern"
    # founder@gmail.com is off the company domain -> confidence reduced
    assert emails["founder@gmail.com"]["confidence"] < 0.85

def test_build_contacts_crawled_beats_pattern():
    found = [{"email": "info@acme.com", "source_page": "home"}]
    out = contacts.build_contacts("acme.com", found)
    info = [c for c in out if c["email"] == "info@acme.com"][0]
    assert info["source"] == "crawl"  # not overwritten by the generated pattern


# --- Phase 9: verification logic -------------------------------------------

def test_syntax_validation():
    assert verify.is_valid_syntax("a@b.com")
    assert not verify.is_valid_syntax("not-an-email")
    assert not verify.is_valid_syntax("a@@b.com")

def test_disposable_detection():
    assert verify.is_disposable("mailinator.com")
    assert not verify.is_disposable("acme.com")

def test_rcpt_code_mapping():
    assert verify.classify_rcpt_code(250) == "accepted"
    assert verify.classify_rcpt_code(550) == "rejected"
    assert verify.classify_rcpt_code(451) == verify.STATUS_UNKNOWN
    assert verify.classify_rcpt_code(None) == verify.STATUS_UNKNOWN

def test_verify_invalid_syntax_and_disposable():
    v = verify.Verifier()
    assert v.verify("garbage") == verify.STATUS_INVALID
    assert v.verify("x@mailinator.com") == verify.STATUS_DISPOSABLE


# --- Phase 10: scoring ------------------------------------------------------

def test_score_full_house():
    facts = {k: True for k in score.SCORE_FACTORS}
    total, breakdown = score.score_company(facts)
    # The spec's factors sum to 95 (ceiling is stated as 100), so a perfect
    # company scores 95 and is clamped at <=100.
    assert total == 95
    assert breakdown["has_verified_email"] == 30

def test_score_partial():
    facts = {"has_website": True, "https": True, "active_website": True}
    total, breakdown = score.score_company(facts)
    assert total == 20  # 10 + 5 + 5
    assert set(breakdown) == {"has_website", "https", "active_website"}

def test_score_empty():
    total, breakdown = score.score_company({})
    assert total == 0 and breakdown == {}


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed ({len(fns)} tests)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
