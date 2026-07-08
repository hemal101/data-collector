"""Tests for Phases 4-6: tech detection, extraction, crawl helpers, hosting.

Run:  .venv/bin/python tests/test_enrich.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import crawl, discovery, extract


# --- Technology detection ---------------------------------------------------

def test_detect_wordpress():
    html = '<html><head><link href="/wp-content/themes/x/style.css"></head></html>'
    techs = {t["technology"] for t in extract.detect_technologies(html)}
    assert "WordPress" in techs

def test_detect_shopify():
    html = '<script src="https://cdn.shopify.com/s/files/1/app.js"></script>'
    assert "Shopify" in {t["technology"] for t in extract.detect_technologies(html)}

def test_detect_nextjs_and_react():
    html = '<div id="__next"></div><script src="/_next/static/chunks/main.js"></script><div data-reactroot></div>'
    techs = {t["technology"] for t in extract.detect_technologies(html)}
    assert "Next.js" in techs and "React" in techs

def test_detect_google_analytics_id():
    html = '<script>gtag("config","G-ABC1234XYZ")</script>'
    techs = {t["technology"] for t in extract.detect_technologies(html)}
    assert "Google Analytics" in techs

def test_detect_cloudflare_from_header():
    techs = {t["technology"] for t in extract.detect_technologies("<html></html>", {"server": "cloudflare"})}
    assert "Cloudflare" in techs

def test_detect_laravel():
    html = '<meta name="csrf-token" content="x"><script src="/vendor/laravel/app.js"></script>'
    assert "Laravel" in {t["technology"] for t in extract.detect_technologies(html)}


# --- Extraction -------------------------------------------------------------

HOME = """
<html><head>
<title>Acme Robotics</title>
<meta name="description" content="We build   autonomous  robots.">
<meta property="og:site_name" content="Acme Robotics Pvt Ltd">
<meta property="og:image" content="https://acme.com/logo.png">
<script type="application/ld+json">
{"@type":"Organization","name":"Acme Robotics","foundingDate":"2018-05-01",
 "address":{"streetAddress":"12 MG Road","addressLocality":"Bengaluru","postalCode":"560001"}}
</script>
</head><body>
<p>Founded in 2018, we are a team of 45 employees.</p>
<a href="mailto:hello@acme.com">Email</a>
<a href="tel:+91 98765 43210">Call</a>
<a href="https://www.linkedin.com/company/acme">LinkedIn</a>
<a href="https://twitter.com/intent/tweet?text=x">Share</a>
<a href="https://twitter.com/acme">Twitter</a>
<a href="https://github.com/acme">GitHub</a>
Contact us at info@acme.com or careers@acme.com.
</body></html>
"""

def test_extract_identity_fields():
    e = extract.extract_from_pages({"home": HOME})
    assert e["extracted_name"] == "Acme Robotics Pvt Ltd"
    assert e["description"] == "We build autonomous robots."
    assert e["logo_url"] == "https://acme.com/logo.png"
    assert e["founded_year"] == 2018
    assert "45" in e["employee_count"]
    assert "Bengaluru" in (e["address"] or "")

def test_extract_emails_and_phones():
    e = extract.extract_from_pages({"home": HOME})
    emails = {x["value"] for x in e["emails"]}
    assert {"hello@acme.com", "info@acme.com", "careers@acme.com"} <= emails
    phones = {x["value"] for x in e["phones"]}
    assert "+919876543210" in phones

def test_extract_socials_skip_share_links():
    e = extract.extract_from_pages({"home": HOME})
    assert e["socials"]["linkedin"] == "https://www.linkedin.com/company/acme"
    assert e["socials"]["twitter"] == "https://twitter.com/acme"  # not the /intent share
    assert e["socials"]["github"] == "https://github.com/acme"

def test_extract_socials_skip_bare_and_nonprofile():
    html = """
    <a href="https://www.facebook.com/">fb root</a>
    <a href="https://www.youtube.com/watch?v=abc">a video</a>
    <a href="https://www.instagram.com/realco/">profile</a>
    """
    e = extract.extract_from_pages({"home": html})
    assert "facebook" not in e["socials"]      # bare root skipped
    assert "youtube" not in e["socials"]        # /watch skipped
    assert e["socials"]["instagram"] == "https://www.instagram.com/realco/"

def test_extract_filters_junk_emails():
    html = '<a href="mailto:logo@2x.png">x</a> real@company.com noreply@example.com'
    e = extract.extract_from_pages({"home": html})
    emails = {x["value"] for x in e["emails"]}
    assert "real@company.com" in emails
    assert not any("example.com" in x for x in emails)
    assert not any(".png" in x for x in emails)


# --- Crawl helpers ----------------------------------------------------------

def test_discover_page_urls_same_site_only():
    html = """
    <a href="/about-us">About</a>
    <a href="/contact">Contact</a>
    <a href="https://other.com/careers">Careers elsewhere</a>
    <a href="https://acme.com/blog/post-1">Blog</a>
    """
    urls = crawl.discover_page_urls("https://acme.com/", html, "acme.com")
    assert urls["about"] == "https://acme.com/about-us"
    assert urls["contact"] == "https://acme.com/contact"
    assert urls["blog"] == "https://acme.com/blog/post-1"
    assert "careers" not in urls  # other.com is off-site

def test_store_and_read_roundtrip(tmp_path=None):
    import tempfile
    root = tempfile.mkdtemp()
    rel, size = crawl.store_html(root, "acme.com", "home", b"<html>hi</html>")
    assert size > 0
    assert crawl.read_stored_html(root, rel) == "<html>hi</html>"


# --- Hosting detection ------------------------------------------------------

def test_hosting_github_pages_by_ip():
    assert discovery.detect_hosting_provider(["185.199.108.153"], [], None, None) == "GitHub Pages"

def test_hosting_cloudflare_by_ns():
    assert discovery.detect_hosting_provider(["1.2.3.4"], ["kate.ns.cloudflare.com"], None, None) == "Cloudflare"

def test_hosting_fallback_to_server_header():
    assert discovery.detect_hosting_provider(["1.2.3.4"], [], None, "nginx/1.18") == "nginx"


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
