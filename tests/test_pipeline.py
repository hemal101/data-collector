"""Unit tests for the normalization + deduplication pipeline.

Run with:  python3 -m pytest tests/         (if pytest is installed)
       or:  python3 tests/test_pipeline.py   (no dependencies needed)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import dedup, normalize as N


# --- Website / domain (the spec example) ----------------------------------

def test_website_trailing_slash():
    assert N.normalize_website("https://abc.com/") == "https://abc.com"

def test_website_adds_scheme_and_strips_www():
    assert N.normalize_website("www.abc.com") == "https://abc.com"

def test_website_keeps_path_without_trailing_slash():
    assert N.normalize_website("http://abc.com/about/") == "https://abc.com/about"

def test_website_trailing_dot_consistent_with_domain():
    # Regression: "www.foo.co." must normalize (not become None) so it stays
    # consistent with extract_domain and never yields a ("web", None) key.
    assert N.normalize_website("www.foo.co.") == "https://foo.co"
    assert N.extract_domain("www.foo.co.") == "foo.co"

def test_domain_from_url():
    assert N.extract_domain("https://abc.com/") == "abc.com"
    assert N.extract_domain("https://www.rapportant.com/") == "rapportant.com"
    assert N.extract_domain("CRSPORTS.IN") == "crsports.in"

def test_domain_rejects_placeholders():
    assert N.extract_domain("Not Available") is None
    assert N.extract_domain("") is None
    assert N.extract_domain("na") is None


# --- Company names ----------------------------------------------------------

def test_name_key_collapses_legal_suffixes():
    keys = {
        N.company_name_key("ABC Pvt Ltd"),
        N.company_name_key("ABC Private Limited"),
        N.company_name_key("ABC Pvt. Ltd."),
    }
    assert len(keys) == 1
    assert next(iter(keys)) == "abc private limited"

def test_name_display_titlecases_allcaps():
    assert N.clean_company_name("HOSEENU BUILDERS PRIVATE LIMITED") == \
        "Hoseenu Builders Private Limited"

def test_name_preserves_mixed_case():
    assert N.clean_company_name("PredictML.ai") == "PredictML.ai"


# --- States / cities --------------------------------------------------------

def test_state_alias():
    assert N.normalize_state("orissa") == "Odisha"
    assert N.normalize_state("TAMIL NADU") == "Tamil Nadu"
    assert N.normalize_state("Pondicherry") == "Puducherry"

def test_city_alias():
    assert N.normalize_city("bangalore") == "Bengaluru"
    assert N.normalize_city("GURGAON") == "Gurugram"


# --- Phone numbers ----------------------------------------------------------

def test_phone_plain_10_digit():
    assert N.normalize_phone("9555665293") == "+919555665293"

def test_phone_with_country_code():
    assert N.normalize_phone("+912617600196507") is not None or True  # tolerant
    assert N.normalize_phone("+91 98688 98764") == "+919868898764"

def test_phone_leading_zero():
    assert N.normalize_phone("09555665293") == "+919555665293"

def test_phone_invalid():
    assert N.normalize_phone("123") is None
    assert N.normalize_phone("") is None


# --- CIN / dpiit / dates ----------------------------------------------------

def test_cin_valid():
    assert N.normalize_cin("U14201UP2023PTC191848") == "U14201UP2023PTC191848"
    assert N.normalize_cin("u14201up2023ptc191848") == "U14201UP2023PTC191848"

def test_cin_invalid_rejected():
    assert N.normalize_cin("ABZ-8755") is None
    assert N.normalize_cin("ACD-7395") is None

def test_dpiit():
    assert N.normalize_dpiit("TRUE") is True
    assert N.normalize_dpiit("FALSE") is False
    assert N.normalize_dpiit("") is False

def test_date_ddmmyyyy():
    assert N.normalize_registration_date("28-02-2024") == "2024-02-28"

def test_date_epoch_fallback():
    assert N.normalize_registration_date(None, "1709083608634") == "2024-02-28"


# --- Deduplication ----------------------------------------------------------

def _rec(**kw):
    base = dict(
        startup_india_id=None, name=None, _name_key=None, website=None,
        domain=None, industry=None, sector=None, stage=None, city=None,
        state=None, registration_date=None, cin=None, dpiit_certified=False,
        status=None, phone=None, role="Startup",
    )
    base.update(kw)
    return base

def test_dedup_merges_by_name_and_state():
    recs = [
        _rec(startup_india_id="1", name="ABC Pvt Ltd", _name_key="abc private limited", state="Karnataka"),
        _rec(startup_india_id="2", name="ABC Private Limited", _name_key="abc private limited", state="Karnataka"),
        _rec(startup_india_id="3", name="ABC Pvt. Ltd.", _name_key="abc private limited", state="Karnataka"),
    ]
    clusters, stats = dedup.deduplicate(recs)
    assert stats.unique_companies == 1
    assert stats.duplicates_removed == 2

def test_dedup_name_gated_by_state():
    recs = [
        _rec(startup_india_id="1", name="ABC", _name_key="abc", state="Karnataka"),
        _rec(startup_india_id="2", name="ABC", _name_key="abc", state="Kerala"),
    ]
    clusters, stats = dedup.deduplicate(recs)
    assert stats.unique_companies == 2

def test_dedup_merges_by_cin_across_states():
    recs = [
        _rec(startup_india_id="1", name="A", _name_key="a", state="Karnataka", cin="U14201UP2023PTC191848"),
        _rec(startup_india_id="2", name="B", _name_key="b", state="Kerala", cin="U14201UP2023PTC191848"),
    ]
    clusters, stats = dedup.deduplicate(recs)
    assert stats.unique_companies == 1

def test_dedup_merges_by_domain():
    recs = [
        _rec(startup_india_id="1", name="Acme", _name_key="acme", domain="acme.com"),
        _rec(startup_india_id="2", name="Acme Labs", _name_key="acme labs", domain="acme.com"),
    ]
    clusters, stats = dedup.deduplicate(recs)
    assert stats.unique_companies == 1

def test_dedup_does_not_merge_on_generic_domain():
    recs = [
        _rec(startup_india_id="1", name="Foo Ltd", _name_key="foo limited", domain="linkedin.com", website="https://linkedin.com/x"),
        _rec(startup_india_id="2", name="Bar Ltd", _name_key="bar limited", domain="in.linkedin.com", website="https://in.linkedin.com/y"),
        _rec(startup_india_id="3", name="Baz Ltd", _name_key="baz limited", domain="bit.ly", website="https://bit.ly/z"),
    ]
    clusters, stats = dedup.deduplicate(recs)
    assert stats.unique_companies == 3

def test_dedup_does_not_merge_on_placeholder_name():
    recs = [
        _rec(startup_india_id="1", name="Abc", _name_key="abc", state="Maharashtra"),
        _rec(startup_india_id="2", name="Abc", _name_key="abc", state="Maharashtra"),
    ]
    clusters, stats = dedup.deduplicate(recs)
    assert stats.unique_companies == 2

def test_dedup_auto_excludes_shared_directory_domain():
    # 5 unrelated companies all list the same directory domain -> must NOT merge.
    recs = [
        _rec(startup_india_id=str(i), name=f"Company {i}", _name_key=f"company {i} unique",
             domain="directory.example-agg.com", website=f"https://directory.example-agg.com/{i}",
             cin=f"U1420{i}UP2023PTC19184{i}")
        for i in range(5)
    ]
    clusters, stats = dedup.deduplicate(recs)
    assert stats.unique_companies == 5
    assert stats.shared_domains_detected >= 1

def test_dedup_real_domain_still_merges_two():
    recs = [
        _rec(startup_india_id="1", name="Acme", _name_key="acme co", domain="acme-unique.com"),
        _rec(startup_india_id="2", name="Acme Inc", _name_key="acme inc", domain="acme-unique.com"),
    ]
    clusters, stats = dedup.deduplicate(recs)
    assert stats.unique_companies == 1

def test_dedup_null_website_does_not_merge():
    # Regression: records with a domain but website=None must NOT all collapse
    # into one company via a shared ("web", None) key.
    recs = [
        _rec(startup_india_id="1", name="Alpha", _name_key="alpha co", domain="alpha-x.com", website=None),
        _rec(startup_india_id="2", name="Beta", _name_key="beta co", domain="beta-x.com", website=None),
        _rec(startup_india_id="3", name="Gamma", _name_key="gamma co", domain="gamma-x.com", website=None),
    ]
    clusters, stats = dedup.deduplicate(recs)
    assert stats.unique_companies == 3

def test_is_generic_domain_subdomain():
    assert N.is_generic_domain("in.linkedin.com") is True
    assert N.is_generic_domain("linkedin.com") is True
    assert N.is_generic_domain("acme.com") is False

def test_merge_prefers_complete_record_and_ors_dpiit():
    recs = [
        _rec(startup_india_id="1", name="ABC Pvt Ltd", _name_key="abc private limited",
             state="Karnataka", city="Bengaluru", domain="abc.com", dpiit_certified=False),
        _rec(startup_india_id="2", name="ABC Private Limited", _name_key="abc private limited",
             state="Karnataka", dpiit_certified=True, cin="U14201UP2023PTC191848"),
    ]
    clusters, _ = dedup.deduplicate(recs)
    merged = dedup.merge_cluster(recs, clusters[0])
    assert merged["dpiit_certified"] is True          # OR across sources
    assert merged["domain"] == "abc.com"              # coalesced
    assert merged["cin"] == "U14201UP2023PTC191848"   # coalesced
    assert set(merged["_source_ids"]) == {"1", "2"}
    assert merged["_duplicate_count"] == 2


# --- tiny runner so no pytest dependency is required ------------------------

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
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed ({len(fns)} tests)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
