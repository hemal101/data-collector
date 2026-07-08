"""Phase 7 - AI (or heuristic) enrichment.

Generates, per company: short_description, industry, sub_industry, ICP,
business_type, target_market, keywords.

If an LLM key is configured (see ``pipeline/llm.py``) we ask the model for a
structured JSON answer. Otherwise we fall back to a deterministic heuristic
built from the Startup India taxonomy + crawled description + detected tech.
"""

from __future__ import annotations

import re

_B2C_SIGNS = (
    "shop", "store", "ecommerce", "e-commerce", "consumer", "fashion", "apparel",
    "food delivery", "restaurant", "retail", "d2c", "beauty", "cosmetic",
    "grocery", "wellness", "fitness", "travel", "jewel", "handmade", "clothing",
    "personal care", "home decor", "toys", "pets",
)
_B2B_SIGNS = (
    "enterprise", "saas", "platform", "api", "b2b", "solution", "software",
    "consulting", "analytics", "infrastructure", "developer", "logistics",
    "manufacturing", "wholesale", "automation", "erp", "crm", "compliance",
    "supply chain", "fintech", "cybersecurity", "cloud",
)
_MARKETPLACE_SIGNS = ("marketplace", "aggregator", "on-demand", "connects buyers")

# High-level industry -> a default ICP when we can't do better.
_ICP_BY_INDUSTRY = {
    "ai": "Enterprises",
    "enterprise software": "Enterprises",
    "finance technology": "SMBs & financial institutions",
    "fintech": "SMBs & financial institutions",
    "healthcare & lifesciences": "Healthcare providers",
    "education": "Students & institutions",
    "edtech": "Students & institutions",
    "retail": "Consumers",
    "fashion": "Consumers",
    "food & beverages": "Consumers",
    "logistics": "Businesses shipping goods",
    "agriculture": "Farmers & agribusinesses",
    "real estate": "Property buyers & developers",
}

_STOPWORDS = set(
    "the a an and or for of to in on with your our we is are be by from at as it "
    "that this you they their his her its all can will more most best your you're "
    "company startup private limited llp india based services solutions".split()
)


def _text(company: dict) -> str:
    parts = [
        company.get("name") or "",
        company.get("industry") or "",
        company.get("sector") or "",
        company.get("description") or "",
    ]
    return " ".join(parts).lower()


def _business_type(company: dict) -> str:
    hay = _text(company)
    if any(s in hay for s in _MARKETPLACE_SIGNS):
        return "Marketplace"
    b2c = sum(s in hay for s in _B2C_SIGNS)
    b2b = sum(s in hay for s in _B2B_SIGNS)
    if b2c > b2b:
        return "B2C"
    if b2b > b2c:
        return "B2B"
    # Tie-break on the coarse industry.
    industry = (company.get("industry") or "").lower()
    if any(k in industry for k in ("retail", "fashion", "food", "consumer", "beauty")):
        return "B2C"
    return "B2B"


def _icp(company: dict, business_type: str) -> str:
    industry = (company.get("industry") or "").lower()
    for key, icp in _ICP_BY_INDUSTRY.items():
        if key in industry:
            return icp
    return "Consumers" if business_type == "B2C" else "SMBs"


def _target_market(company: dict, business_type: str) -> str:
    stage = (company.get("stage") or "").lower()
    geo = "Global" if stage in ("scaling", "growth") else "India"
    segment = {
        "B2C": "consumers",
        "B2B": "businesses",
        "Marketplace": "buyers and sellers",
    }.get(business_type, "businesses")
    industry = company.get("industry") or ""
    return f"{geo} {industry} {segment}".replace("  ", " ").strip()


def _keywords(company: dict, limit: int = 8) -> list[str]:
    kws: list[str] = []
    for field in ("industry", "sector"):
        v = company.get(field)
        if v:
            kws.append(v)
    for tech in company.get("technologies", []) or []:
        name = tech["technology"] if isinstance(tech, dict) else tech
        if name in ("Shopify", "WordPress"):
            kws.append(name)
    desc = (company.get("description") or "").lower()
    tokens = re.findall(r"[a-z][a-z0-9\-]{3,}", desc)
    freq: dict[str, int] = {}
    for t in tokens:
        if t in _STOPWORDS:
            continue
        freq[t] = freq.get(t, 0) + 1
    for word, _ in sorted(freq.items(), key=lambda kv: -kv[1]):
        kws.append(word)
        if len(kws) >= limit:
            break
    # De-dupe case-insensitively, preserve order.
    seen, out = set(), []
    for k in kws:
        kl = k.lower()
        if kl not in seen:
            seen.add(kl)
            out.append(k)
    return out[:limit]


def _short_description(company: dict) -> str | None:
    desc = company.get("description")
    if desc:
        d = re.sub(r"\s+", " ", desc).strip()
        return d[:200].rsplit(" ", 1)[0] if len(d) > 200 else d
    name = company.get("name")
    if not name:
        return None
    industry = company.get("industry") or "technology"
    city = company.get("city")
    stage = company.get("stage")
    loc = f" based in {city}" if city else ""
    stg = f"{stage.lower()}-stage " if stage else ""
    return f"{name} is a {stg}{industry} company{loc}.".replace("  ", " ")


def heuristic_enrich(company: dict) -> dict:
    business_type = _business_type(company)
    return {
        "short_description": _short_description(company),
        "industry": company.get("industry"),
        "sub_industry": company.get("sector"),
        "icp": _icp(company, business_type),
        "business_type": business_type,
        "target_market": _target_market(company, business_type),
        "keywords": _keywords(company),
        "model": "heuristic",
    }


_SYSTEM = (
    "You are a precise B2B company data enrichment engine. Given structured "
    "facts about a company, infer concise marketing-segmentation attributes. "
    "Respond ONLY with a JSON object with keys: short_description (<=200 chars), "
    "industry, sub_industry, icp, business_type (one of B2B, B2C, B2B2C, "
    "Marketplace), target_market, keywords (array of 3-8 short strings)."
)


def _llm_user_payload(company: dict) -> str:
    import json
    fields = {
        k: company.get(k)
        for k in ("name", "industry", "sector", "stage", "city", "state", "description", "website")
        if company.get(k)
    }
    techs = [t["technology"] if isinstance(t, dict) else t for t in company.get("technologies", []) or []]
    if techs:
        fields["technologies"] = techs
    return json.dumps(fields, ensure_ascii=False)


def enrich(company: dict, llm=None) -> dict:
    """Enrich one company. Uses the LLM if provided, else the heuristic."""
    if llm is not None:
        data = llm.complete_json(_SYSTEM, _llm_user_payload(company))
        if data:
            return {
                "short_description": _s(data.get("short_description")),
                "industry": _s(data.get("industry")) or company.get("industry"),
                "sub_industry": _s(data.get("sub_industry")) or company.get("sector"),
                "icp": _s(data.get("icp")),
                "business_type": _s(data.get("business_type")),
                "target_market": _s(data.get("target_market")),
                "keywords": _kw(data.get("keywords")),
                "model": llm.model,
            }
    return heuristic_enrich(company)


def _s(v) -> str | None:
    if v is None:
        return None
    v = str(v).strip()
    return v[:250] or None


def _kw(v) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()][:8]
    if isinstance(v, str):
        return [p.strip() for p in v.split(",") if p.strip()][:8]
    return []
