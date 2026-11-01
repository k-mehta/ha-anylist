"""Local recipe search and unchanged read-action regression coverage."""

from types import SimpleNamespace

import pytest
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.anylist import async_setup
from custom_components.anylist.client import Ingredient, Recipe, RecipeCollection
from custom_components.anylist.recipe_search import search_recipes, search_store
from custom_components.anylist.store import StoreError, open_store
from custom_components.anylist.sync import sync_recipes, sync_snapshot

from .conftest import FakeRecipe
from .test_integration import _attach_runtime, _mock_entry


SALMON = FakeRecipe("exact-anylist-ID", "Pink Horseradish & Dill Salmon")


@pytest.mark.parametrize(
    "query",
    [
        "pink horseradish salmon",
        "salmon pink horseradish",
        "  PINK, horseradish---SALMON!  ",
        "ＰＩＮＫ horseradish salmon",
        "𝐏𝐈𝐍𝐊 horseradish salmon",
        "pink horseardish salmon",
        "pink horserdish salmon",
        "pink horserradish salmon",
        "pink horseradish salman",
    ],
)
def test_salmon_words_and_typos(query) -> None:
    """The reported title remains first in a synthetic 412-recipe collection."""
    recipes = [FakeRecipe(f"other-{i}", f"Weeknight Pasta {i}") for i in range(410)]
    recipes += [FakeRecipe("partial", "Pink Salmon"), SALMON]
    result = search_recipes(recipes, query)
    assert result["recipes"][0]["id"] == SALMON.id
    assert result["recipes"][0]["name"] == SALMON.name
    assert result["count"] == 2
    assert result["has_more"] is False


def test_ranking_tiers_and_long_titles() -> None:
    """Exact and all-word title tiers beat partial, typo and ingredient matches."""
    recipes = [
        FakeRecipe("partial", "Pink Salmon"),
        FakeRecipe("typo", "Pink Horshradish Salmon"),
        FakeRecipe(
            "ingredient",
            "Dinner",
            ingredients=[SimpleNamespace(name="pink horseradish salmon")],
        ),
        FakeRecipe(
            "long", "The Best Pink Oven Roasted Horseradish and Dill Salmon for Dinner"
        ),
        FakeRecipe("exact", "Pink horseradish salmon"),
    ]
    result = search_recipes(
        recipes, "pink horseradish salmon", include_ingredients=True
    )
    assert [recipe["id"] for recipe in result["recipes"]] == [
        "exact",
        "long",
        "typo",
        "partial",
        "ingredient",
    ]
    assert result["recipes"][0]["match_explanation"] == "Exact normalized title"
    assert result["recipes"][1]["match_explanation"] == "All query words in title"
    assert "1 typo matches" in result["recipes"][2]["match_explanation"]
    assert "2/3 words" in result["recipes"][3]["match_explanation"]


@pytest.mark.parametrize(
    "title", ["Crème brûlée", "Cre\u0300me bru\u0302le\u0301e", "CRÈME—BRÛLÉE"]
)
def test_unicode_equivalence(title) -> None:
    """Canonical, accent, case and punctuation differences normalize alike."""
    assert (
        search_recipes([FakeRecipe("dessert", title)], "creme brulee")["recipes"][0][
            "score"
        ]
        == 100
    )


def test_ingredients_are_opt_in_and_names_only() -> None:
    """Ingredients can supplement titles without exposing recipe contents."""
    dinner = FakeRecipe(
        "dinner",
        "Pink Dinner",
        ingredients=[
            SimpleNamespace(name="horseradish", note="salmon", raw_ingredient="salmon")
        ],
        preparation_steps=["salmon"],
        note="salmon",
    )
    assert search_recipes([dinner], "pink horseradish")["recipes"][0]["score"] == 40
    result = search_recipes([dinner], "pink horseradish", include_ingredients=True)
    assert result["recipes"][0]["score"] > 40
    assert "ingredients: 1/2" in result["recipes"][0]["match_explanation"]
    assert search_recipes([dinner], "horseradish")["count"] == 0
    assert (
        search_recipes([dinner], "horseradish", include_ingredients=True)["count"] == 1
    )
    assert (
        search_recipes([dinner], "horseardish", include_ingredients=True)["count"] == 1
    )
    assert search_recipes([dinner], "salmon", include_ingredients=True)["count"] == 0
    assert set(result["recipes"][0]) == {
        "id", "name", "score", "match_explanation", "collections"
    }


@pytest.mark.parametrize(
    "query", ["chocolate cake", "xyzzy", "pin", "pink chocolate cheesecake"]
)
def test_unrelated_and_insufficient_matches(query) -> None:
    """Do not pad results with unrelated or weakly overlapping titles."""
    assert search_recipes([SALMON], query) == {
        "recipes": [],
        "count": 0,
        "has_more": False,
    }


def test_repeated_query_words_do_not_inflate_relevance() -> None:
    """Repeated words count once and a fuzzy word cannot reuse an exact match."""
    assert search_recipes([SALMON], "pink pink salmon")["recipes"][0]["score"] == 90
    result = search_recipes([FakeRecipe("one", "salmon")], "salmon salmom")
    assert result["recipes"][0]["score"] == 40


def test_limits_and_stable_ties() -> None:
    """Ties sort by normalized title then exact ID, independent of input order."""
    recipes = [FakeRecipe(f"id-{i:02}", "Salmon Dinner") for i in reversed(range(52))]
    recipes.append(FakeRecipe("first", "Salmon Bake"))
    for limit in (1, 15, 50):
        result = search_recipes(recipes, "salmon", limit=limit)
        assert result == search_recipes(reversed(recipes), "salmon", limit=limit)
        assert result["count"] == limit
        assert result["has_more"] is True
        assert [recipe["id"] for recipe in result["recipes"]] == ["first"] + [
            f"id-{i:02}" for i in range(limit - 1)
        ]
    assert search_recipes(recipes[:15], "salmon")["has_more"] is False
    assert search_recipes(recipes, "salmon")["count"] == 15


async def _setup(hass):
    entry = _mock_entry()
    entry.add_to_hass(hass)
    client, _ = _attach_runtime(hass, entry)
    client.recipes = [SALMON]
    _seed_store(entry, client)
    assert await async_setup(hass, {})
    return client, entry


def _seed_store(entry, client) -> None:
    """Sync the entry's in-memory store from the fake client's snapshot.

    The read actions serve from the store, so every service test seeds
    it the way the coordinator does — one snapshot, no live download.
    The seeding download is setup, not measured behavior, so the
    fake's call log is cleared afterwards.
    """
    store = open_store(":memory:")
    sync_snapshot(store, client.get_user_data())
    client.calls.clear()
    entry.runtime_data.store = store


async def _call(hass, data, action="search_recipes"):
    return await hass.services.async_call(
        "anylist", action, data, blocking=True, return_response=True
    )


async def test_search_then_read_and_legacy_list_behavior(hass: HomeAssistant) -> None:
    """Search returns exact IDs while list substring and read defaults stay intact."""
    client, entry = await _setup(hass)
    client.recipes = [
        FakeRecipe(
            SALMON.id,
            SALMON.name,
            ingredients=[SimpleNamespace(name="salmon")],
            preparation_steps=["Bake"],
        )
    ]
    _seed_store(entry, client)
    result = await _call(hass, {"query": "pink horseradish salmon"})
    assert result == search_recipes(client.recipes, "pink horseradish salmon")
    assert client.calls == []
    recipe_id = result["recipes"][0]["id"]
    detail = await _call(hass, {"recipe_id": recipe_id}, "get_recipe")
    assert detail["recipe"]["id"] == SALMON.id
    assert detail["recipe"]["preparation_steps"] == ["Bake"]
    assert detail["recipe"]["ingredients"][0]["name"] == "salmon"
    assert (await _call(hass, {"name": SALMON.name}, "get_recipe")) == detail
    with pytest.raises(HomeAssistantError):
        await _call(hass, {"name": SALMON.name.lower()}, "get_recipe")
    assert await _call(hass, {"query": "pink horseradish salmon"}, "get_recipes") == {
        "recipes": []
    }
    listed = await _call(hass, {"query": "HORSERADISH & DILL"}, "get_recipes")
    assert set(listed) == {"recipes"}
    assert listed["recipes"][0]["ingredients"] == detail["recipe"]["ingredients"]
    assert listed["recipes"][0]["preparation_steps"] == []
    assert await _call(hass, {}, "get_recipes") == listed
    assert await _call(hass, {"query": ""}, "get_recipes") == listed
    stripped = await _call(
        hass,
        {"recipe_id": recipe_id, "include_ingredients": False, "include_steps": False},
        "get_recipe",
    )
    assert stripped["recipe"]["ingredients"] == []
    assert stripped["recipe"]["preparation_steps"] == []


@pytest.mark.parametrize("query", ["", "   \t\n", "& -- !!!", "\u0301"])
async def test_empty_query_rejected_before_retrieval(hass, query) -> None:
    client, _ = await _setup(hass)
    with pytest.raises(HomeAssistantError) as error:
        await _call(hass, {"query": query})
    assert error.value.translation_key == "empty_search_query"
    assert client.calls == []


@pytest.mark.parametrize(
    "data",
    [{}, {"query": None}]
    + [
        {"query": "salmon", "limit": limit}
        for limit in (
            0,
            -1,
            51,
            1.5,
            True,
            False,
            "many",
            None,
            float("nan"),
            float("inf"),
        )
    ],
)
async def test_invalid_inputs(hass, data) -> None:
    client, _ = await _setup(hass)
    with pytest.raises(vol.Invalid):
        await _call(hass, data)
    assert client.calls == []


async def test_service_options_and_multiple_accounts(hass) -> None:
    first, _ = await _setup(hass)
    second_entry = _mock_entry()
    second_entry.add_to_hass(hass)
    second, _ = _attach_runtime(hass, second_entry)
    second.recipes = [
        FakeRecipe(str(i), "Dinner", ingredients=[SimpleNamespace(name="salmon")])
        for i in range(20)
    ]
    _seed_store(second_entry, second)
    with pytest.raises(HomeAssistantError) as error:
        await _call(hass, {"query": "salmon"})
    assert error.value.translation_key == "multiple_entries"
    data = {
        "config_entry_id": second_entry.entry_id,
        "query": "salmon",
        "include_ingredients": True,
    }
    assert (await _call(hass, data))["count"] == 15
    result = await _call(hass, {**data, "limit": 1.0})
    assert result["count"] == 1
    assert result["has_more"] is True
    assert first.calls == []
    assert (await _call(hass, {**data, "include_ingredients": False}))["count"] == 0
    with pytest.raises(HomeAssistantError) as error:
        await _call(hass, {**data, "config_entry_id": "missing"})
    assert error.value.translation_key == "config_entry_not_found"


async def test_retrieval_errors_use_existing_translation(hass) -> None:
    """A closed recipe cache fails search with the load-failed translation."""
    _, entry = await _setup(hass)
    entry.runtime_data.store.close()
    with pytest.raises(HomeAssistantError) as error:
        await _call(hass, {"query": "salmon"})
    assert error.value.translation_key == "recipes_load_failed"
    assert isinstance(error.value.__cause__, StoreError)


# ---------------------------------------------------------------------------
# Store-backed search (design §7): the design prototype's cases, run
# against the real store + search layer instead of the prototype's
# throwaway SQLite schema.
# ---------------------------------------------------------------------------

# (id, name, ingredient names, prep seconds, cook seconds, servings, rating)
_PROTOTYPE_TABLE = [
    ("r01", "10 Minute Air Fryer Kale Chips", [], 300, 300, "4", 5),
    ("r02", "15 Minute Lo Mein", [], 300, 900, "4", 4),
    (
        "r03",
        "15-minute honey garlic chicken thighs with sesame and scallions",
        [],
        300,
        900,
        "4",
        5,
    ),
    ("r04", "15-Minute Miso Soup", [], 300, 600, "2 (bowls)", 4),
    (
        "r05",
        "Carrot Cake with Cream Cheese Frosting",
        ["carrots", "flour", "sugar", "cream cheese", "walnuts"],
        1200,
        2400,
        "12",
        5,
    ),
    (
        "r06",
        "Roasted Carrot Soup with Ginger and Crispy Chickpeas",
        ["carrots", "ginger", "chickpeas", "vegetable stock"],
        600,
        1800,
        "6",
        4,
    ),
    ("r07", "Carrot and Coriander Fritters", [], 900, 600, "4", 3),
    (
        "r08",
        "Creamy Tomato Pasta",
        ["penne", "cherry tomatoes", "cream", "basil"],
        600,
        1200,
        "4",
        5,
    ),
    (
        "r09",
        "Weeknight Pasta with Cherry Tomatoes and Basil",
        ["pasta", "cherry tomatoes", "basil", "garlic"],
        300,
        900,
        "4",
        4,
    ),
    ("r10", "Slow-Cooked Beef and Carrot Stew", [], 1800, 7200, "6", 5),
    ("r11", "Salt", [], 0, 0, "", None),
    ("r12", "Pesto Pasta", [], 300, 600, "2", 4),
    ("r13", "Mushroom Risotto", [], 600, 1800, "4", 5),
    ("r14", "Honey Garlic Salmon", [], 600, 900, "2", 5),
    ("r15", "Carrot Top Pesto", [], 600, 300, "1 jar", 3),
]

_WEEKNIGHT = RecipeCollection(
    id="weeknight", name="Weeknight", recipe_ids=["r08", "r09", "r12"]
)
_EMPTY_COLLECTION = RecipeCollection(id="empty", name="Empty", recipe_ids=[])


def _prototype_recipes() -> list[Recipe]:
    """The prototype's recipes as client objects, with collections set."""
    recipes = []
    for recipe_id, name, ingredients, prep, cook, servings, rating in (
        _PROTOTYPE_TABLE
    ):
        recipes.append(
            Recipe(
                id=recipe_id,
                name=name,
                ingredients=[Ingredient(name=item) for item in ingredients],
                prep_time=prep,
                cook_time=cook,
                servings=servings,
                rating=rating,
                collections=(
                    [_WEEKNIGHT]
                    if recipe_id in _WEEKNIGHT.recipe_ids
                    else []
                ),
            )
        )
    return recipes


def _prototype_store():
    """An open in-memory store seeded with the prototype's recipes."""
    store = open_store(":memory:")
    sync_recipes(store, _prototype_recipes(), [_WEEKNIGHT, _EMPTY_COLLECTION])
    return store


@pytest.mark.parametrize(
    "query,expected",
    [
        ("carr", ["r07", "r05", "r15", "r06", "r10"]),
        ("carro", ["r07", "r05", "r15", "r06", "r10"]),
        ("carrot", ["r07", "r05", "r15", "r06", "r10"]),
        ("carot", ["r07", "r05", "r15", "r06", "r10"]),
        ("carrto", ["r07", "r05", "r15", "r06", "r10"]),
        ("carrot cake", ["r05", "r07", "r15", "r06", "r10"]),
        ("15 minute honey garlic", ["r03", "r02", "r04", "r14"]),
        ("kale", ["r01"]),
        ("pasta", ["r08", "r12", "r09"]),
        ("xyzz", []),
        ("ca", ["r07", "r05", "r15", "r06", "r10"]),
        # The prototype expects [r02, r03, r04] here because it sorts by
        # the raw lowercased name ("15 minute..." < "15-minute..."). The
        # integration sorts by the normalized name, as the old scorer
        # does, so the hyphenated r03 sorts first.
        ("15", ["r03", "r02", "r04"]),
    ],
)
def test_search_store_prototype_cases(query, expected) -> None:
    """Prefix, typo, multi-word, short-word, and no-match prototype cases."""
    with _prototype_store() as store:
        result = search_store(store, query)
    assert [recipe["id"] for recipe in result["recipes"]] == expected


@pytest.mark.parametrize(
    "query,flag,expected",
    [
        ("basil", False, ["r09"]),
        ("basil", True, ["r09", "r08"]),
        ("cherry tomatoes", True, ["r09", "r08"]),
    ],
)
def test_search_store_ingredient_flag(query, flag, expected) -> None:
    """Ingredient search is opt-in and finds no stemming ("tomato" != "tomatoes")."""
    with _prototype_store() as store:
        result = search_store(store, query, include_ingredients=flag)
    assert [recipe["id"] for recipe in result["recipes"]] == expected


def test_search_store_scoring_tiers() -> None:
    """Exact title 100, all title words 90, typo 64, coverage at most 80."""
    with _prototype_store() as store:
        exact = search_store(store, "carrot and coriander fritters")
        assert exact["recipes"][0]["score"] == 100.0
        assert exact["recipes"][0]["match_explanation"] == "Exact normalized title"
        all_words = search_store(store, "carrot")
        assert all_words["recipes"][0]["score"] == 90.0
        assert (
            all_words["recipes"][0]["match_explanation"]
            == "All query words in title"
        )
        typo = search_store(store, "carot")
        assert typo["recipes"][0]["score"] == 64.0
        assert "1 typo matches" in typo["recipes"][0]["match_explanation"]
        partial = search_store(store, "carrot cake")
        assert partial["recipes"][1]["score"] == 40.0
        assert (
            partial["recipes"][1]["match_explanation"]
            == "Title: 1/2 words (0 typo matches)"
        )


def test_search_store_word_counts_once_across_title_and_ingredients() -> None:
    """A word in both the title and the ingredients scores once, as before.

    "basil garlic" on the Weeknight Pasta: basil is in the title and
    the ingredients, garlic only in the ingredients. The old scorer
    consumed basil in the title, so the coverage is (1 + 0.45) / 2 =
    58.0 — not the 76.0 a double count would give.
    """
    with _prototype_store() as store:
        result = search_store(store, "basil garlic", include_ingredients=True)
    assert result["recipes"][0]["id"] == "r09"
    assert result["recipes"][0]["score"] == 58.0


def test_search_store_collection_scope() -> None:
    """A collection scope keeps only its members, ranked the same way."""
    with _prototype_store() as store:
        scoped = search_store(
            store, "pasta", recipe_ids=frozenset(_WEEKNIGHT.recipe_ids)
        )
        assert [recipe["id"] for recipe in scoped["recipes"]] == [
            "r08",
            "r12",
            "r09",
        ]
        empty = search_store(
            store, "pasta", recipe_ids=frozenset(_EMPTY_COLLECTION.recipe_ids)
        )
        assert empty == {"recipes": [], "count": 0, "has_more": False}
        for recipe in scoped["recipes"]:
            assert recipe["collections"] == [
                {"id": "weeknight", "name": "Weeknight"}
            ]


def test_search_store_limits_and_empty_query() -> None:
    """Limits slice, has_more reports the overflow, empty queries fail."""
    with _prototype_store() as store:
        limited = search_store(store, "carrot", limit=2)
        assert limited["count"] == 2
        assert limited["has_more"] is True
        assert len(limited["recipes"]) == 2
        full = search_store(store, "carrot")
        assert full["count"] == 5
        assert full["has_more"] is False
        with pytest.raises(ValueError):
            search_store(store, "   ")


# Queries where the old in-memory scorer and the store search must agree
# exactly, plus the two deliberate differences: prefix/substring
# matching ("carr", "ca") and two query words landing on one title word
# ("carrot carot" — the old scorer consumed the word once, the store
# cannot tell which title word an FTS hit landed on).
_PARITY_QUERIES = [
    ("carrot", False),
    ("carrot cake", False),
    ("15 minute honey garlic", False),
    ("15", False),
    ("kale", False),
    ("pasta", False),
    ("xyzz", False),
    ("basil", True),
    ("basil garlic", True),
    ("cherry tomatoes", True),
    ("carot", False),
    ("carrto", False),
]


@pytest.mark.parametrize("query,include_ingredients", _PARITY_QUERIES)
def test_search_store_parity_with_in_memory_search(
    query, include_ingredients
) -> None:
    """The store search returns exactly what the old scorer returned."""
    recipes = _prototype_recipes()
    with _prototype_store() as store:
        assert search_store(
            store, query, include_ingredients=include_ingredients
        ) == search_recipes(
            recipes, query, include_ingredients=include_ingredients
        )


@pytest.mark.parametrize(
    "query,expected_ids",
    [
        ("carr", ["r07", "r05", "r15", "r06", "r10"]),
        ("ca", ["r07", "r05", "r15", "r06", "r10"]),
    ],
)
def test_search_store_prefix_differs_from_in_memory_search(
    query, expected_ids
) -> None:
    """Prefix matching is the intended difference: the old scorer found nothing."""
    recipes = _prototype_recipes()
    assert search_recipes(recipes, query)["recipes"] == []
    with _prototype_store() as store:
        result = search_store(store, query)
    assert [recipe["id"] for recipe in result["recipes"]] == expected_ids


def test_search_store_two_query_words_one_title_word() -> None:
    """The other intended difference: "carrot carot" counts both words.

    The old scorer takes exact matches first, so "carrot" consumes
    the title word and "carot" never matches (80 * 1 / 2 = 40.0); the
    store counts the exact word and the typo-corrected word
    separately (80 * (1 + 0.8) / 2 = 72.0).
    """
    recipes = _prototype_recipes()
    old = search_recipes(recipes, "carrot carot")
    assert old["recipes"][0]["id"] == "r07"
    assert old["recipes"][0]["score"] == 40.0
    with _prototype_store() as store:
        new = search_store(store, "carrot carot")
    assert new["recipes"][0]["id"] == "r07"
    assert new["recipes"][0]["score"] == 72.0
