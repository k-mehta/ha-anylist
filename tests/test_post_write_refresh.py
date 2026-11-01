"""Unit tests for the post-write refresh (``sync.py`` + service helper).

A write that succeeds against AnyList must be visible to the next
store read immediately — without waiting for the next poll and
without another snapshot download. These tests drive the sync-layer
refresh functions directly against an in-memory store with client
``Recipe`` objects (no protobuf, no Home Assistant instance except
for the service-helper tests, which use the ``hass`` fixture).

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
    refresh_deleted_recipe,
    refresh_recipe,
    sync_recipes,
    updated_recipe,
)


def _recipe(
    recipe_id: str,
    name: str,
    *,
    ingredients: list[Ingredient] | None = None,
    steps: list[str] | None = None,
    note: str | None = None,
    servings: str | None = None,
    prep_time: int | None = None,
    cook_time: int | None = None,
    rating: int | None = None,
    photo_urls: list[str] | None = None,
    collections: list[RecipeCollection] | None = None,
) -> Recipe:
    """Build a client recipe with the fields a write touches."""
    return Recipe(
        id=recipe_id,
        name=name,
        ingredients=ingredients or [],
        preparation_steps=steps or [],
        note=note,
        servings=servings,
        prep_time=prep_time,
        cook_time=cook_time,
        rating=rating,
        photo_urls=photo_urls or [],
        collections=collections or [],
    )


def _seeded_store():
    """A store with two recipes, one of them in a collection."""
    store = open_store(":memory:")
    weeknight = RecipeCollection(
        id="weeknight", name="Weeknight", recipe_ids=["r08"]
    )
    sync_recipes(
        store,
        [
            _recipe(
                "r08",
                "Creamy Tomato Pasta",
                ingredients=[Ingredient(name="penne")],
                steps=["Boil the pasta"],
                servings="4",
                prep_time=600,
                cook_time=1200,
                rating=5,
                note="Family favourite",
                collections=[weeknight],
            ),
            _recipe("r15", "Carrot Top Pesto", steps=["Blend"]),
        ],
        [weeknight],
    )
    return store


def test_refresh_recipe_upserts_created_recipe():
    """A created recipe is readable from the store right after refresh."""
    store = _seeded_store()
    created = _recipe(
        "r99",
        "Rig Test Soup",
        ingredients=[Ingredient(name="carrots", quantity="2")],
        steps=["Simmer"],
    )
    assert refresh_recipe(store, created) is True
    stored = store.get_recipe("r99")
    assert stored is not None
    assert stored.name == "Rig Test Soup"
    assert [entry.name for entry in stored.ingredients] == ["carrots"]
    assert stored.steps == ["Simmer"]
    assert [recipe.id for recipe in store.list_recipes()] == [
        "r08",
        "r15",
        "r99",
    ]
    title_hits, _ = store.search_words("soup")
    assert title_hits == {"r99"}


def test_refresh_recipe_upserts_recipe_into_collection():
    """A refreshed recipe keeps its collection membership."""
    store = _seeded_store()
    weeknight = RecipeCollection(
        id="weeknight", name="Weeknight", recipe_ids=["r08", "r99"]
    )
    created = _recipe("r99", "Rig Test Soup", collections=[weeknight])
    assert refresh_recipe(store, created) is True
    stored = store.get_recipe("r99")
    assert stored is not None
    assert stored.collections == ["weeknight"]
    assert [recipe.id for recipe in store.list_recipes(collection_id="weeknight")] == [
        "r08",
        "r99",
    ]


def test_refresh_recipe_unchanged_when_nothing_changed():
    """Refreshing the same recipe twice writes once (hash short-circuit)."""
    store = _seeded_store()
    same = _recipe(
        "r08",
        "Creamy Tomato Pasta",
        ingredients=[Ingredient(name="penne")],
        steps=["Boil the pasta"],
        servings="4",
        prep_time=600,
        cook_time=1200,
        rating=5,
        note="Family favourite",
        collections=[
            RecipeCollection(id="weeknight", name="Weeknight", recipe_ids=["r08"])
        ],
    )
    assert refresh_recipe(store, same) is False


def test_updated_recipe_replaces_only_scoped_fields():
    """An update replaces name/ingredients/steps, preserves the rest."""
    original = _recipe(
        "r08",
        "Creamy Tomato Pasta",
        ingredients=[Ingredient(name="penne")],
        steps=["Boil the pasta"],
        servings="4",
        prep_time=600,
        cook_time=1200,
        rating=5,
        note="Family favourite",
        photo_urls=["https://photos.anylist.com/old-photo.jpg"],
        collections=[
            RecipeCollection(id="weeknight", name="Weeknight", recipe_ids=["r08"])
        ],
    )
    changed = updated_recipe(
        original,
        name="Creamy Tomato Pasta (Updated)",
        ingredients=[Ingredient(name="rigatoni", quantity="500 g")],
        preparation_steps=["Boil the rigatoni", "Toss with sauce"],
        photo_id=None,
    )
    assert changed.id == "r08"
    assert changed.name == "Creamy Tomato Pasta (Updated)"
    assert [entry.name for entry in changed.ingredients] == ["rigatoni"]
    assert changed.preparation_steps == ["Boil the rigatoni", "Toss with sauce"]
    # Everything outside the update's scope is preserved.
    assert changed.servings == "4"
    assert changed.prep_time == 600
    assert changed.cook_time == 1200
    assert changed.rating == 5
    assert changed.note == "Family favourite"
    assert changed.photo_urls == ["https://photos.anylist.com/old-photo.jpg"]
    assert [c.id for c in changed.collections] == ["weeknight"]
    # The original is untouched.
    assert original.name == "Creamy Tomato Pasta"


def test_updated_recipe_replaces_photo_only_when_supplied():
    """A new photo replaces the photo URLs; otherwise they are kept."""
    original = _recipe(
        "r08",
        "Creamy Tomato Pasta",
        photo_urls=["https://photos.anylist.com/old-photo.jpg"],
    )
    with_photo = updated_recipe(
        original,
        name="Creamy Tomato Pasta",
        ingredients=[],
        preparation_steps=[],
        photo_id="new-photo",
    )
    assert with_photo.photo_urls == ["https://photos.anylist.com/new-photo.jpg"]
    without_photo = updated_recipe(
        original,
        name="Creamy Tomato Pasta",
        ingredients=[],
        preparation_steps=[],
        photo_id=None,
    )
    assert without_photo.photo_urls == ["https://photos.anylist.com/old-photo.jpg"]


def test_refresh_after_update_visible_immediately():
    """An updated recipe reads back new, and the old name is gone."""
    store = _seeded_store()
    original = store.get_recipe("r08")
    assert original is not None
    # Rebuild the client recipe the way the service layer does: the
    # stored row mapped back, then the update applied.
    from custom_components.anylist.sync import data_to_recipe

    client_recipe = data_to_recipe(
        original,
        {"weeknight": RecipeCollection(id="weeknight", name="Weeknight")},
    )
    changed = updated_recipe(
        client_recipe,
        name="Creamy Tomato Pasta (Updated)",
        ingredients=[Ingredient(name="rigatoni", quantity="500 g")],
        preparation_steps=["Boil the rigatoni"],
        photo_id=None,
    )
    assert refresh_recipe(store, changed) is True
    stored = store.get_recipe("r08")
    assert stored is not None
    assert stored.name == "Creamy Tomato Pasta (Updated)"
    assert [entry.name for entry in stored.ingredients] == ["rigatoni"]
    assert stored.steps == ["Boil the rigatoni"]
    # The old name no longer matches, the new one does.
    assert store.get_recipe_by_name("Creamy Tomato Pasta") is None
    assert store.get_recipe_by_name("Creamy Tomato Pasta (Updated)") is not None
    title_hits, _ = store.search_words("updated")
    assert title_hits == {"r08"}


def test_refresh_deleted_recipe_removes_it():
    """A deleted recipe is gone from the store right after refresh."""
    store = _seeded_store()
    assert refresh_deleted_recipe(store, "r15") is True
    assert store.get_recipe("r15") is None
    assert [recipe.id for recipe in store.list_recipes()] == ["r08"]
    title_hits, _ = store.search_words("carrot")
    assert title_hits == set()
    # Deleting an id the store never held is a quiet no-op.
    assert refresh_deleted_recipe(store, "r15") is False
    assert refresh_deleted_recipe(store, "never-existed") is False


async def test_refresh_store_after_write_success(hass: HomeAssistant):
    """The service helper upserts through the executor."""
    store = _seeded_store()
    await _async_refresh_store_after_write(
        hass,
        store,
        recipe=_recipe("r99", "Rig Test Soup", steps=["Simmer"]),
    )
    assert store.get_recipe("r99") is not None
    await _async_refresh_store_after_write(hass, store, recipe_id="r15")
    assert store.get_recipe("r15") is None


async def test_refresh_store_after_write_never_fails_the_write(
    hass: HomeAssistant,
):
    """A broken store is logged, never raised: the write stands."""
    store = _seeded_store()
    store.close()
    await _async_refresh_store_after_write(
        hass,
        store,
        recipe=_recipe("r99", "Rig Test Soup"),
    )
    await _async_refresh_store_after_write(hass, store, recipe_id="r15")


async def test_refresh_store_after_write_noop_without_store(hass: HomeAssistant):
    """No store (tests, or an entry set up before the cache) is a no-op."""
    await _async_refresh_store_after_write(
        hass,
        None,
        recipe=_recipe("r99", "Rig Test Soup"),
    )
    await _async_refresh_store_after_write(hass, None, recipe_id="r15")
