"""Phase 3 - Deduplication.

Companies appear across multiple datasets, so the same real-world company can
show up many times. We link records that share any strong identifier and treat
each connected group as ONE company.

Match keys (in decreasing strength):
    - startup_india_id  (exact)
    - cin               (exact, validated format)
    - domain            (exact)
    - website           (exact, normalized URL)
    - company name key  (exact, suffix-normalized) *gated by state*

Name matching is the weakest signal, so two records only link on name when
they also share the same normalized state. This prevents merging two unrelated
companies that happen to share a common name in different states.

Linking is transitive via union-find: A~B and B~C  =>  {A, B, C} is one company.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from pipeline.normalize import is_generic_domain, is_generic_name_key


class UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        # Path compression.
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


# A domain is treated as non-identifying (shared directory / marketplace /
# aggregator) if it is attached to at least this many distinct company names
# or this many distinct valid CINs. One real company almost never spans this.
_SHARED_DOMAIN_NAME_THRESHOLD = 4
_SHARED_DOMAIN_CIN_THRESHOLD = 2


@dataclass
class DedupStats:
    total_records: int = 0
    unique_companies: int = 0
    duplicates_removed: int = 0
    links_by_key: dict[str, int] = field(default_factory=dict)
    largest_cluster: int = 0
    shared_domains_detected: int = 0


def _detect_shared_domains(records: list[dict]) -> set[str]:
    """Find domains that clearly belong to many companies (so must not link).

    Uses the data itself: a domain seen with >=N distinct company names or
    >=M distinct CINs is a shared/aggregator domain, not a company's own site.
    """
    names: dict[str, set] = defaultdict(set)
    cins: dict[str, set] = defaultdict(set)
    for r in records:
        d = r["domain"]
        if not d or is_generic_domain(d):
            continue
        if r["_name_key"]:
            names[d].add(r["_name_key"])
        if r["cin"]:
            cins[d].add(r["cin"])
    shared = set()
    for d in names.keys() | cins.keys():
        if (
            len(names.get(d, ())) >= _SHARED_DOMAIN_NAME_THRESHOLD
            or len(cins.get(d, ())) >= _SHARED_DOMAIN_CIN_THRESHOLD
        ):
            shared.add(d)
    return shared


def _link_by(
    uf: UnionFind,
    records: list[dict],
    key_fn,
) -> int:
    """Union all records that produce the same non-null key. Returns #links."""
    buckets: dict[object, int] = {}
    links = 0
    for i, rec in enumerate(records):
        key = key_fn(rec)
        if key is None:
            continue
        if key in buckets:
            uf.union(buckets[key], i)
            links += 1
        else:
            buckets[key] = i
    return links


def deduplicate(records: list[dict]) -> tuple[list[list[int]], DedupStats]:
    """Cluster ``records`` (already-normalized dicts) into companies.

    Returns a list of clusters (each a list of indices into ``records``) and
    stats. Every input record belongs to exactly one cluster.
    """
    n = len(records)
    uf = UnionFind(n)
    stats = DedupStats(total_records=n)

    shared_domains = _detect_shared_domains(records)
    stats.shared_domains_detected = len(shared_domains)

    def _domain_usable(r: dict) -> bool:
        d = r["domain"]
        return bool(d) and not is_generic_domain(d) and d not in shared_domains

    stats.links_by_key["startup_india_id"] = _link_by(
        uf, records, lambda r: ("sid", r["startup_india_id"]) if r["startup_india_id"] else None
    )
    stats.links_by_key["cin"] = _link_by(
        uf, records, lambda r: ("cin", r["cin"]) if r["cin"] else None
    )
    stats.links_by_key["domain"] = _link_by(
        uf, records, lambda r: ("dom", r["domain"]) if _domain_usable(r) else None
    )
    stats.links_by_key["website"] = _link_by(
        uf, records, lambda r: ("web", r["website"]) if r["website"] and _domain_usable(r) else None
    )
    stats.links_by_key["name+state"] = _link_by(
        uf,
        records,
        lambda r: ("name", r["_name_key"], r["state"])
        if not is_generic_name_key(r["_name_key"])
        else None,
    )

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        clusters[uf.find(i)].append(i)

    cluster_list = list(clusters.values())
    stats.unique_companies = len(cluster_list)
    stats.duplicates_removed = n - len(cluster_list)
    stats.largest_cluster = max((len(c) for c in cluster_list), default=0)
    return cluster_list, stats


# ---------------------------------------------------------------------------
# Merging a cluster into a single canonical company record
# ---------------------------------------------------------------------------

# Fields where we simply take the best (first non-null, richest) value.
_COALESCE_FIELDS = [
    "startup_india_id", "name", "website", "domain", "industry", "sector",
    "stage", "city", "state", "registration_date", "cin", "status", "phone",
    "role",
]


def _completeness(rec: dict) -> int:
    """How many meaningful fields are populated - used to pick the primary."""
    return sum(1 for k in _COALESCE_FIELDS if rec.get(k))


def merge_cluster(records: list[dict], indices: Iterable[int]) -> dict:
    """Collapse a cluster of raw records into one canonical company dict."""
    members = [records[i] for i in indices]
    # Primary = most complete record; ties broken by having a CIN, then by the
    # earliest registration date (oldest registration usually = the real one).
    primary = max(
        members,
        key=lambda r: (
            _completeness(r),
            1 if r.get("cin") else 0,
            1 if r.get("dpiit_certified") else 0,
        ),
    )

    merged: dict = {}
    for field_name in _COALESCE_FIELDS:
        value = primary.get(field_name)
        if not value:
            # Fall back to any other member that has the field.
            for m in members:
                if m.get(field_name):
                    value = m[field_name]
                    break
        merged[field_name] = value or None

    # dpiit_certified: TRUE if ANY source says certified.
    merged["dpiit_certified"] = any(m.get("dpiit_certified") for m in members)
    # registration_date: prefer the earliest known date across the cluster.
    dates = sorted(m["registration_date"] for m in members if m.get("registration_date"))
    if dates:
        merged["registration_date"] = dates[0]

    merged["_source_ids"] = [m["startup_india_id"] for m in members if m.get("startup_india_id")]
    merged["_duplicate_count"] = len(members)
    return merged
