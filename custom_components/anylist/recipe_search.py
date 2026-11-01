"""Local, word-based recipe ranking.

``search_recipes`` ranks in-memory recipes (the original implementation,
kept as the reference the store-backed search is checked against).
``search_store`` runs the same ranking against the SQLite cache
(design §7): FTS5 trigram prefix matches, a LIKE fallback for short
words, and one-edit vocabulary correction for true typos. The service
layer calls ``search_store``; nothing here downloads a snapshot.
"""

from __future__ import annotations

from collections.abc import Iterable
import unicodedata
from typing import TYPE_CHECKING, Any

from .store import RecipeStore

if TYPE_CHECKING:
    from .client import Recipe

DEFAULT_SEARCH_LIMIT = 15
MAX_SEARCH_LIMIT = 50


def normalize_search_text(text: str) -> str:
    """Fold Unicode/case/accents and separate punctuation into words."""
    text = unicodedata.normalize("NFKD", text).casefold()
    return " ".join(
        "".join(
            char if char.isalnum() else " "
            for char in text
            if not unicodedata.category(char).startswith("M")
        ).split()
    )


def _is_minor_typo(first: str, second: str) -> bool:
    """Allow one insertion, deletion, substitution or adjacent transposition.

    Short words require exact matches to avoid broad false positives. This
    compares individual words, so extra title words never reduce relevance.
    """
    if min(len(first), len(second)) < 4 or abs(len(first) - len(second)) > 1:
        return False
    if len(first) > len(second):
        first, second = second, first
    index = next(
        (index for index, char in enumerate(first) if char != second[index]),
        len(first),
    )
    if len(first) != len(second):
        return first[index:] == second[index + 1 :]
    return first[index + 1 :] == second[index + 1 :] or (
        index + 1 < len(first)
        and first[index] == second[index + 1]
        and first[index + 1] == second[index]
        and first[index + 2 :] == second[index + 2 :]
    )


def _take_matches(query: set[str], words: set[str], *, fuzzy: bool = False) -> set[str]:
    """Consume distinct matching words in stable order, without double counting."""
    matched = set()
    for word in sorted(query):
        match = next(
            (
                candidate
                for candidate in sorted(words)
                if (_is_minor_typo(word, candidate) if fuzzy else word == candidate)
            ),
            None,
        )
        if match is not None:
            matched.add(word)
            words.remove(match)
    query.difference_update(matched)
    return matched


def search_recipes(
    recipes: Iterable[Recipe],
    query: str,
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
    include_ingredients: bool = False,
) -> dict[str, Any]:
    """Return compact candidates; scores are relevance, not confidence.

    Exact normalized titles score 100, all exact title words score 90, and
    remaining matches score at most 80 by query coverage. Title exact/typo
    weights are 1/0.8; ingredient exact/typo weights are 0.45/0.3. Require at
    least half the unique query words and never pad to the requested limit.
    """
    normalized_query = normalize_search_text(query)
    if not normalized_query:
        raise ValueError("Query must contain at least one letter or number")
    query_words = set(normalized_query.split())
    total = len(query_words)
    candidates = []
    for recipe in recipes:
        title = normalize_search_text(recipe.name)
        title_words = set(title.split())
        remaining = query_words.copy()
        title_exact = _take_matches(remaining, title_words)
        title_fuzzy = _take_matches(remaining, title_words, fuzzy=True)
        ingredient_exact: set[str] = set()
        ingredient_fuzzy: set[str] = set()
        if include_ingredients and remaining:
            ingredient_words = {
                word
                for ingredient in recipe.ingredients
                for word in normalize_search_text(ingredient.name or "").split()
            }
            ingredient_exact = _take_matches(remaining, ingredient_words)
            ingredient_fuzzy = _take_matches(remaining, ingredient_words, fuzzy=True)
        if (total - len(remaining)) * 2 < total:
            continue

        if title == normalized_query:
            score = 100.0
            explanation = "Exact normalized title"
        elif len(title_exact) == total:
            score = 90.0
            explanation = "All query words in title"
        else:
            score = round(
                80
                * (
                    len(title_exact)
                    + 0.8 * len(title_fuzzy)
                    + 0.45 * len(ingredient_exact)
                    + 0.3 * len(ingredient_fuzzy)
                )
                / total,
                2,
            )
            explanation = (
                f"Title: {len(title_exact) + len(title_fuzzy)}/{total} words"
                f" ({len(title_fuzzy)} typo matches)"
            )
            if ingredient_exact or ingredient_fuzzy:
                explanation += (
                    f"; ingredients: {len(ingredient_exact) + len(ingredient_fuzzy)}"
                    f"/{total} words ({len(ingredient_fuzzy)} typo matches)"
                )
        candidates.append(
            {
                "id": recipe.id,
                "name": recipe.name,
                "score": score,
                "match_explanation": explanation,
                "collections": [
                    {"id": value.id, "name": value.name}
                    for value in (getattr(recipe, "collections", []) or [])
                ],
            }
        )

    candidates.sort(
        key=lambda candidate: (
            -candidate["score"],
            normalize_search_text(candidate["name"]),
            candidate["id"],
        )
    )
    return {
        "recipes": candidates[:limit],
        "count": min(len(candidates), limit),
        "has_more": len(candidates) > limit,
    }


def _correct_word(word: str, vocabulary: set[str]) -> str | None:
    """The vocabulary word one edit away from ``word``, if there is one.

    A correction only counts when exactly one vocabulary word is one
    edit away — two or more candidates means the query is genuinely
    ambiguous, and no correction is applied (the prototype's rule).
    """
    candidates = [
        candidate for candidate in vocabulary if _is_minor_typo(word, candidate)
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def search_store(
    store: RecipeStore,
    query: str,
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
    include_ingredients: bool = False,
    recipe_ids: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Search the SQLite cache (design §7), with the old scoring.

    Two stages, exactly as the design's prototype: per-word FTS5
    trigram hits — a typed prefix matches, so "carr" finds carrot
    recipes, and words under 3 characters use the store's word-boundary
    prefix LIKE fallback — then a one-edit vocabulary correction for
    words with no hit at all ("carot" still finds carrot). Ranking
    reproduces the in-memory ``search_recipes`` scoring: exact
    normalized title 100, all query words in the title 90, otherwise
    up to 80 by coverage with weights title 1.0 / title-typo 0.8 /
    ingredient 0.45 / ingredient-typo 0.3, and the half-word rule (a
    recipe must match at least half the query words).

    Two deliberate differences from the in-memory scorer, both pinned
    down by the parity test: a query word is counted once per recipe —
    in the title if it matched there, otherwise in the ingredients —
    so a word in both places never scores twice (the old scorer
    consumed each word the same way); and the store cannot say *which*
    title word an FTS hit landed on, so two different query words can
    both count against the same title word ("carrot carot" scores
    both, where the old scorer consumed the word once).
    """
    normalized_query = normalize_search_text(query)
    if not normalized_query:
        raise ValueError("Query must contain at least one letter or number")
    query_words = sorted(set(normalized_query.split()))
    total = len(query_words)

    title_hits: dict[str, set[str]] = {}
    ingredient_hits: dict[str, set[str]] = {}
    for word in query_words:
        title_hits[word], ingredient_hits[word] = store.search_words(
            word, include_ingredients=include_ingredients
        )

    typo_words: set[str] = set()
    missed = [
        word
        for word in query_words
        if not title_hits[word] and not ingredient_hits[word]
    ]
    if missed:
        vocabulary = store.get_vocabulary()
        for word in missed:
            corrected = _correct_word(word, vocabulary)
            if corrected is None:
                continue
            typo_words.add(word)
            title_hits[word], ingredient_hits[word] = store.search_words(
                corrected, include_ingredients=include_ingredients
            )

    recipe_collections: dict[str, list[dict[str, str]]] = {}
    for collection in store.list_collections():
        for recipe_id in collection.recipe_ids:
            recipe_collections.setdefault(recipe_id, []).append(
                {"id": collection.id, "name": collection.name}
            )

    names = store.list_recipe_names()
    candidates: list[dict[str, Any]] = []
    for recipe_id in {
        hit
        for word in query_words
        for hit in title_hits[word] | ingredient_hits[word]
        if recipe_ids is None or hit in recipe_ids
    }:
        identity = names.get(recipe_id)
        if identity is None:
            continue
        title_exact = {
            word
            for word in query_words
            if recipe_id in title_hits[word] and word not in typo_words
        }
        title_typo = {
            word
            for word in query_words
            if recipe_id in title_hits[word] and word in typo_words
        }
        ingredient_exact = {
            word
            for word in query_words
            if recipe_id in ingredient_hits[word]
            and word not in typo_words
            and word not in title_exact
            and word not in title_typo
        }
        ingredient_typo = {
            word
            for word in query_words
            if recipe_id in ingredient_hits[word]
            and word in typo_words
            and word not in title_exact
            and word not in title_typo
        }
        matched = (
            len(title_exact)
            + len(title_typo)
            + len(ingredient_exact)
            + len(ingredient_typo)
        )
        if matched * 2 < total:
            continue
        if identity.name_normalized == normalized_query:
            score, explanation = 100.0, "Exact normalized title"
        elif len(title_exact) == total:
            score, explanation = 90.0, "All query words in title"
        else:
            score = round(
                80
                * (
                    len(title_exact)
                    + 0.8 * len(title_typo)
                    + 0.45 * len(ingredient_exact)
                    + 0.3 * len(ingredient_typo)
                )
                / total,
                2,
            )
            explanation = (
                f"Title: {len(title_exact) + len(title_typo)}/{total} words"
                f" ({len(title_typo)} typo matches)"
            )
            if ingredient_exact or ingredient_typo:
                explanation += (
                    f"; ingredients: "
                    f"{len(ingredient_exact) + len(ingredient_typo)}"
                    f"/{total} words ({len(ingredient_typo)} typo matches)"
                )
        candidates.append(
            {
                "id": recipe_id,
                "name": identity.name,
                "score": score,
                "match_explanation": explanation,
                "collections": recipe_collections.get(recipe_id, []),
            }
        )

    candidates.sort(
        key=lambda candidate: (
            -candidate["score"],
            normalize_search_text(candidate["name"]),
            candidate["id"],
        )
    )
    return {
        "recipes": candidates[:limit],
        "count": min(len(candidates), limit),
        "has_more": len(candidates) > limit,
    }
