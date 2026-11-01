"""Unit tests for the collection-membership refresh (``sync.py`` + service helper).

Adding or removing a recipe from a collection is a write, so the
store must show the new membership immediately — without waiting for
the next poll and without another snapshot download. These tests
drive the sync-layer refresh function directly against an in-memory
store (no protobuf, no Home Assistant instance except for the
service-helper tests, which use the ``hass`` fixture).

Run as part of the repo suite:
``test-venv/bin/python -m pytest tests/ -o asyncio_mode=auto``.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.anylist import _async_refresh_store_after_write
from custom_components.anylist.client import (
    Ingredient,
    Recipe,
    RecipeCollection,
)
from custom_components.anylist.store import open_store
from custom_components.anylist.sync import (
    refresh_recipe_membership,
    sync_recipes,
)


def _recipe(
    recipe_id: str,
    name: str,
    *,
    collections: list[RecipeCollection] | None = None,
) -> Recipe:
    """Build a client recipe with only the fields membership touches."""
    return Recipe(
        id=recipe_id,
        name=name,
        ingredients=[Ingredient(name="penne")],
        preparation_steps=["Boil the pasta"],
        collections=collections or [],
    )


def _seeded_store():
    """A store with two recipes, one of them in a collection."""
    store = open_store(":memory:")
    weeknight = RecipeCollection(
        id="weeknight", name="Weeknight", recipe_ids=["r08"]
    )
    empty = RecipeCollection(id="empty", name="Empty", recipe_ids=[])
    sync_recipes(
        store,
        [
            _recipe("r08", "Creamy Tomato Pasta", collections=[weeknight]),
            _recipe("r15", "Carrot Top Pesto"),
        ],
        [weeknight, empty],
    )
    return store


def test_refresh_membership_adds_recipe_to_collection():
    """Adding a recipe to a collection shows up in the store at once."""
    store = _seeded_store()
    weeknight = RecipeCollection(
        id="weeknight", name="Weeknight", recipe_ids=["r08", "r15"]
    )
    assert refresh_recipe_membership(store, "r15", weeknight, True) is True
    stored = store.get_recipe("r15")
    assert stored is not None
    assert stored.collections == ["weeknight"]
    assert [recipe.id for recipe in store.list_recipes(collection_id="weeknight")] == [
        "r08",
        "r15",
    ]
    collections = {
        collection.id: collection for collection in store.list_collections()
    }
    assert collections["weeknight"].recipe_ids == ["r08", "r15"]
    # The recipe itself is untouched apart from its memberships.
    assert stored.name == "Carrot Top Pesto"
    assert [entry.name for entry in stored.ingredients] == ["penne"]


def test_refresh_membership_adds_unknown_collection_first():
    """Adding to a collection the store never saw still works."""
    store = _seeded_store()
    favourites = RecipeCollection(
        id="favourites", name="Favourites", recipe_ids=["r15"]
    )
    assert refresh_recipe_membership(store, "r15", favourites, True) is True
    stored = store.get_recipe("r15")
    assert stored is not None
    assert stored.collections == ["favourites"]
    collections = {
        collection.id: collection for collection in store.list_collections()
    }
    assert collections["favourites"].name == "Favourites"
    assert collections["favourites"].recipe_ids == ["r15"]


def test_refresh_membership_removes_recipe_from_collection():
    """Removing a recipe from a collection drops it, keeps the recipe."""
    store = _seeded_store()
    weeknight = RecipeCollection(id="weeknight", name="Weeknight", recipe_ids=[])
    assert refresh_recipe_membership(store, "r08", weeknight, False) is True
    stored = store.get_recipe("r08")
    assert stored is not None
    assert stored.collections == []
    assert [recipe.id for recipe in store.list_recipes(collection_id="weeknight")] == []
    collections = {
        collection.id: collection for collection in store.list_collections()
    }
    assert collections["weeknight"].recipe_ids == []
    # The recipe itself is still in the store, unlisted. (The upsert
    # rewrites the row, so it moves to the end of rowid order.)
    assert sorted(recipe.id for recipe in store.list_recipes()) == ["r08", "r15"]
    assert stored.name == "Creamy Tomato Pasta"


def test_refresh_membership_noop_when_already_matching():
    """A membership that already matches (or a missing recipe) is a no-op."""
    store = _seeded_store()
    weeknight = RecipeCollection(
        id="weeknight", name="Weeknight", recipe_ids=["r08"]
    )
    # r08 is already in weeknight; r15 is already out of it.
    assert refresh_recipe_membership(store, "r08", weeknight, True) is False
    assert refresh_recipe_membership(store, "r15", weeknight, False) is False
    # A recipe the store never held has no memberships to update.
    assert refresh_recipe_membership(store, "never-existed", weeknight, True) is False
    assert refresh_recipe_membership(store, "never-existed", weeknight, False) is False
    # Nothing changed.
    assert store.get_recipe("r08").collections == ["weeknight"]
    assert store.get_recipe("r15").collections == []


async def test_refresh_store_after_write_membership(hass: HomeAssistant):
    """The service helper refreshes memberships through the executor."""
    store = _seeded_store()
    weeknight = RecipeCollection(
        id="weeknight", name="Weeknight", recipe_ids=["r08", "r15"]
    )
    await _async_refresh_store_after_write(
        hass,
        store,
        membership_recipe_id="r15",
        membership_collection=weeknight,
        membership_add=True,
    )
    assert store.get_recipe("r15").collections == ["weeknight"]
    await _async_refresh_store_after_write(
        hass,
        store,
        membership_recipe_id="r08",
        membership_collection=RecipeCollection(
            id="weeknight", name="Weeknight", recipe_ids=[]
        ),
        membership_add=False,
    )
    assert store.get_recipe("r08").collections == []


async def test_refresh_store_after_write_membership_never_fails_the_write(
    hass: HomeAssistant,
):
    """A broken store is logged, never raised: the write stands."""
    store = _seeded_store()
    store.close()
    await _async_refresh_store_after_write(
        hass,
        store,
        membership_recipe_id="r15",
        membership_collection=RecipeCollection(
            id="weeknight", name="Weeknight", recipe_ids=["r08", "r15"]
        ),
        membership_add=True,
    )


async def test_refresh_store_after_write_membership_noop_without_store(
    hass: HomeAssistant,
):
    """No store (tests, or an entry set up before the cache) is a no-op."""
    await _async_refresh_store_after_write(
        hass,
        None,
        membership_recipe_id="r15",
        membership_collection=RecipeCollection(
            id="weeknight", name="Weeknight", recipe_ids=["r08", "r15"]
        ),
        membership_add=True,
    )
