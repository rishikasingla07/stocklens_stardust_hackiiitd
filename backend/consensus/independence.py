"""Independence: assigns evidence to independence groups (e.g. syndicated copies share a group).

Two items belong to the same group if they come from the same URL domain (same
original source) or have near-duplicate snippets (same wire story). Groups are
transitive, so ten copies of one story form ONE group. An existing
independence_group is always kept.
"""

import difflib
import re
from urllib.parse import urlparse

from backend import config
from backend.models import Evidence


def domain_of(url: str) -> str:
    """Lower-cased host without a leading 'www.'; '' if the URL has no host."""
    host = urlparse(url).netloc.lower().split(":")[0]
    return host.removeprefix("www.")


def similarity(a: str, b: str) -> float:
    """difflib ratio of two texts after lower-casing and collapsing whitespace."""
    a = re.sub(r"\s+", " ", a.lower()).strip()
    b = re.sub(r"\s+", " ", b.lower()).strip()
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _same_story(a: Evidence, b: Evidence) -> bool:
    domain_a, domain_b = domain_of(a.url), domain_of(b.url)
    if domain_a and domain_a == domain_b:
        return True
    return similarity(a.snippet, b.snippet) >= config.NEAR_DUPLICATE_SIMILARITY


def assign_groups(evidence: list[Evidence]) -> list[Evidence]:
    """Return copies of the evidence with independence_group filled in, in input order."""
    parent = list(range(len(evidence)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(evidence)):
        for j in range(i + 1, len(evidence)):
            if _same_story(evidence[i], evidence[j]):
                parent[find(j)] = find(i)

    # Name each cluster after its first explicit group, else its first member's domain.
    names: dict[int, str] = {}
    for i, item in enumerate(evidence):
        root = find(i)
        if item.independence_group and root not in names:
            names[root] = item.independence_group
    for i, item in enumerate(evidence):
        root = find(i)
        if root not in names:
            names[root] = f"auto:{domain_of(item.url) or item.id}"

    return [
        item if item.independence_group
        else item.model_copy(update={"independence_group": names[find(i)]})
        for i, item in enumerate(evidence)
    ]


def count_groups(evidence: list[Evidence]) -> int:
    """Number of independence groups among the evidence (run assign_groups first)."""
    return len({item.independence_group or f"ungrouped:{item.id}" for item in evidence})
