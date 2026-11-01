"""Unit tests for the AnyList recipe-cache sync (``sync.py``).

These tests run against the real package (the repo suite runs with
Home Assistant installed), with an in-memory store and fake clients
returning canned snapshot bytes built with the client's own protobuf
helpers — the same pattern as ``test_client.py``. No Home Assistant
instance is needed except for the coordinator wiring test, which uses
the ``hass`` fixture.

Run as part of the repo suite:
``test-venv/bin/python -m pytest tests/ -o asyncio_mode=auto``.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.anylist import _async_fetch_data_stages
from custom_components.anylist import client as client_module
from custom_components.anylist.client import (
    AnyListError,
    Ingredient,
    RecipeIngredientSection,
)
from custom_components.anylist.store import open_store
from custom_components.anylist.sync import (
    RecipeSync,
    data_to_recipe,
    recipe_to_data,
    sync_recipes,
    sync_snapshot,
)

from .test_client import (
    _favourites_user_data,
    _recipes_with_headings_user_data,
    _shopping_user_data,
)


def _ingredient_bytes(
    name: str,
    quantity: str | None = None,
    raw: str | None = None,
    note: str | None = None,
) -> bytes:
    """Build one plain-ingredient entry (field 8 of a recipe)."""
    out = b""
    if raw is not None:
        out += client_module._field_string(1, raw)
    out += client_module._field_string(2, name)
    if quantity is not None:
        out += client_module._field_string(3, quantity)
    if note is not None:
        out += client_module._field_string(4, note)
    return out


def _recipe_bytes(
    recipe_id: str,
    name: str,
    ingredients: list[bytes] | None = None,
    steps: list[str] | None = None,
    *,
    note: str | None = None,
    servings: str | None = None,
    prep_time: int | None = None,
    cook_time: int | None = None,
    rating: int | None = None,
) -> bytes:
    """Build one recipe message (field 3 of a recipes response)."""
    out = client_module._field_string(1, recipe_id)
    out += client_module._field_string(3, name)
    if note is not None:
        out += client_module._field_string(5, note)
    for entry in ingredients or []:
        out += client_module._field_message(8, entry)
    for step in steps or []:
        out += client_module._field_string(9, step)
    if rating is not None:
        out += client_module._field_int32(15, rating)
    if cook_time is not None:
        out += client_module._field_int32(18, cook_time)
    if prep_time is not None:
        out += client_module._field_int32(19, prep_time)
    if servings is not None:
        out += client_module._field_string(20, servings)
    return out


def _collection_bytes(
    collection_id: str, name: str, recipe_ids: list[str]
) -> bytes:
    """Build one raw collection (field 5 of a recipes response)."""
    out = client_module._field_string(1, collection_id)
    out += client_module._field_string(3, name)
    for recipe_id in recipe_ids:
        out += client_module._field_string(4, recipe_id)
    return out


def _recipes_snapshot(
    recipes: list[bytes],
    collections: list[bytes] | None = None,
    collection_order: list[str] | None = None,
) -> bytes:
    """Build a top-level snapshot holding a recipes response (field 3)."""
    response = b"".join(
        client_module._field_message(3, recipe) for recipe in recipes
    )
    for collection in collections or []:
        response += client_module._field_message(5, collection)
    for collection_id in collection_order or []:
        response += client_module._field_string(4, collection_id)
    response += client_module._field_string(9, "recipe-data-1")
    return client_module._field_message(3, response)


def _pasta_recipe() -> bytes:
    """Weeknight Pasta with one ingredient and one step."""
    return _recipe_bytes(
        "recipe-1",
        "Weeknight Pasta",
        ingredients=[_ingredient_bytes("Pasta", quantity="1 box", raw="1 box pasta")],
        steps=["Boil water"],
        note="note",
        servings="4",
        prep_time=5,
        cook_time=10,
        rating=5,
    )


def _soup_recipe() -> bytes:
    """Carrot Ginger Soup with two ingredients."""
    return _recipe_bytes(
        "recipe-2",
        "Carrot Ginger Soup",
        ingredients=[
            _ingredient_bytes("Carrots", quantity="500 g"),
            _ingredient_bytes("Ginger", quantity="1 tbsp"),
        ],
        steps=["Simmer everything"],
        servings="6",
        prep_time=10,
        cook_time=30,
        rating=4,
    )


def _two_recipe_snapshot() -> bytes:
    """Two recipes in one collection plus one empty collection."""
    return _recipes_snapshot(
        [_pasta_recipe(), _soup_recipe()],
        collections=[
            _collection_bytes("col-1", "Weeknight", ["recipe-1", "recipe-2"]),
            _collection_bytes("col-2", "Empty", []),
        ],
        collection_order=["col-1", "col-2"],
    )


class _FakeSyncClient:
    """Fake client returning canned snapshots, counting downloads."""

    def __init__(self, snapshots: list[bytes]) -> None:
        self._snapshots = list(snapshots)
        self.calls = 0

    def get_user_data(self) -> bytes:
        """Return the next canned snapshot."""
        self.calls += 1
        if not self._snapshots:
            raise AssertionError("fake client ran out of snapshots")
        return self._snapshots.pop(0)


class _FailingSyncClient:
    """Fake client whose download always fails."""

    def __init__(self) -> None:
        self.calls = 0

    def get_user_data(self) -> bytes:
        """Raise as a failed download would."""
        self.calls += 1
        raise AnyListError("boom")


def test_recipe_to_data_maps_plain_recipe() -> None:
    """A plain client recipe maps verbatim, times in seconds."""
    recipe = client_module.Recipe(
        id="recipe-1",
        name="Weeknight Pasta",
        ingredients=[Ingredient(name="Pasta", quantity="1 box", raw_ingredient="1 box pasta")],
        preparation_steps=["Boil water"],
        note="note",
        servings="4",
        prep_time=300,
        cook_time=600,
        rating=5,
        photo_urls=["https://example.com/photo.jpg"],
        collections=[
            client_module.RecipeCollection(id="col-1", name="Weeknight", recipe_ids=["recipe-1"])
        ],
    )
    data = recipe_to_data(recipe)
    assert data.id == "recipe-1"
    assert data.prep_time_seconds == 300
    assert data.cook_time_seconds == 600
    assert data.servings == "4"
    assert data.collections == ["col-1"]
    assert [(entry.kind, entry.name, entry.quantity) for entry in data.ingredients] == [
        ("ingredient", "Pasta", "1 box")
    ]
    assert data.steps == ["Boil water"]


def test_recipe_to_data_falls_back_without_entries() -> None:
    """Recipes parsed before headings existed use the flat list."""
    recipe = SimpleNamespace(
        id="recipe-1",
        name="Plain",
        ingredients=[Ingredient(name="Salt")],
        preparation_steps=[],
    )
    data = recipe_to_data(recipe)
    assert [(entry.kind, entry.name) for entry in data.ingredients] == [
        ("ingredient", "Salt")
    ]


def test_recipe_to_data_maps_headings_in_order() -> None:
    """Headed entries map to section rows, in order, never as ingredients."""
    recipe = SimpleNamespace(
        id="recipe-headings",
        name="Headed Pasta",
        ingredient_entries=[
            RecipeIngredientSection(name="For the sauce"),
            Ingredient(name="Tomatoes", quantity="500 g"),
            RecipeIngredientSection(name="Topping"),
            Ingredient(name="Basil"),
        ],
        ingredients=[
            Ingredient(name="Tomatoes", quantity="500 g"),
            Ingredient(name="Basil"),
        ],
        preparation_steps=["Simmer the sauce"],
    )
    data = recipe_to_data(recipe)
    assert [(entry.kind, entry.name) for entry in data.ingredients] == [
        ("section", "For the sauce"),
        ("ingredient", "Tomatoes"),
        ("section", "Topping"),
        ("ingredient", "Basil"),
    ]
    assert data.ingredients[1].quantity == "500 g"


def test_headed_recipe_round_trips_through_store() -> None:
    """A headed recipe survives parse → sync → store → read intact.

    The full path, not just the mapping: snapshot bytes with section
    headings are parsed, synced into the store, and read back. The
    rebuilt recipe carries the headings in ``ingredient_entries`` in
    order, and ``ingredients`` stays plain-only — a heading never
    appears as an ingredient.
    """
    store = open_store(":memory:")
    result = sync_snapshot(
        store, _shopping_user_data() + _recipes_with_headings_user_data()
    )
    assert result.recipes_added == 1

    stored = store.get_recipe("recipe-headings")
    assert stored is not None
    assert [(entry.kind, entry.name) for entry in stored.ingredients] == [
        ("section", "For the sauce"),
        ("ingredient", "Tomatoes"),
        ("section", "Topping"),
        ("ingredient", "Basil"),
    ]

    recipe = data_to_recipe(stored, {})
    assert [ingredient.name for ingredient in recipe.ingredients] == [
        "Tomatoes",
        "Basil",
    ]
    assert [type(entry).__name__ for entry in recipe.ingredient_entries] == [
        "RecipeIngredientSection",
        "Ingredient",
        "RecipeIngredientSection",
        "Ingredient",
    ]
    assert [entry.name for entry in recipe.ingredient_entries] == [
        "For the sauce",
        "Tomatoes",
        "Topping",
        "Basil",
    ]
    assert recipe.ingredient_entries[1].quantity == "500 g"


def test_first_sync_populates() -> None:
    """The first sync fills an empty store from one download."""
    store = open_store(":memory:")
    snapshot = _two_recipe_snapshot()
    client = _FakeSyncClient([snapshot])

    result = RecipeSync(store).sync(client)

    assert client.calls == 1
    assert result.recipes_added == 2
    assert result.recipes_updated == 0
    assert result.recipes_unchanged == 0
    assert result.recipes_deleted == 0
    assert result.collections_added == 2
    assert result.snapshot_hash == hashlib.sha256(snapshot).hexdigest()
    assert result.synced_at is not None
    assert store.recipe_count() == 2
    assert store.get_meta("recipe_count") == "2"
    assert store.get_meta("last_snapshot_hash") == result.snapshot_hash
    assert store.get_meta("last_sync_at") == result.synced_at

    pasta = store.get_recipe("recipe-1")
    assert pasta is not None
    assert pasta.name == "Weeknight Pasta"
    assert pasta.prep_time_seconds == 5
    assert pasta.cook_time_seconds == 10
    assert pasta.servings == "4"
    assert pasta.rating == 5
    assert pasta.collections == ["col-1"]
    assert [(entry.kind, entry.name) for entry in pasta.ingredients] == [
        ("ingredient", "Pasta")
    ]
    collections = store.list_collections()
    assert [(c.id, c.name, c.recipe_ids) for c in collections] == [
        ("col-1", "Weeknight", ["recipe-1", "recipe-2"]),
        ("col-2", "Empty", []),
    ]


def test_identical_second_sync_writes_nothing() -> None:
    """An unchanged snapshot rewrites nothing."""
    store = open_store(":memory:")
    snapshot = _two_recipe_snapshot()
    RecipeSync(store).sync(_FakeSyncClient([snapshot]))

    result = RecipeSync(store).sync(_FakeSyncClient([snapshot]))

    assert result.recipes_added == 0
    assert result.recipes_updated == 0
    assert result.recipes_unchanged == 2
    assert result.recipes_deleted == 0
    assert result.collections_added == 0
    assert result.collections_updated == 0
    assert store.recipe_count() == 2


def test_edited_recipe_updates_one_row() -> None:
    """A changed recipe is rewritten; an unchanged one is not."""
    store = open_store(":memory:")
    RecipeSync(store).sync(_FakeSyncClient([_two_recipe_snapshot()]))

    edited = _recipes_snapshot(
        [
            _recipe_bytes(
                "recipe-1",
                "Weeknight Pasta (Updated)",
                ingredients=[
                    _ingredient_bytes("Pasta", quantity="2 boxes", raw="2 boxes pasta")
                ],
                steps=["Boil water"],
                servings="4",
                prep_time=5,
                cook_time=10,
                rating=5,
            ),
            _soup_recipe(),
        ],
        collections=[
            _collection_bytes("col-1", "Weeknight", ["recipe-1", "recipe-2"]),
            _collection_bytes("col-2", "Empty", []),
        ],
        collection_order=["col-1", "col-2"],
    )
    result = RecipeSync(store).sync(_FakeSyncClient([edited]))

    assert result.recipes_added == 0
    assert result.recipes_updated == 1
    assert result.recipes_unchanged == 1
    pasta = store.get_recipe("recipe-1")
    assert pasta is not None
    assert pasta.name == "Weeknight Pasta (Updated)"
    assert pasta.ingredients[0].quantity == "2 boxes"
    assert store.get_recipe("recipe-2") is not None


def test_deleted_recipe_disappears() -> None:
    """A recipe missing from the snapshot leaves the store."""
    store = open_store(":memory:")
    RecipeSync(store).sync(_FakeSyncClient([_two_recipe_snapshot()]))

    only_pasta = _recipes_snapshot(
        [_pasta_recipe()],
        collections=[
            _collection_bytes("col-1", "Weeknight", ["recipe-1"]),
            _collection_bytes("col-2", "Empty", []),
        ],
        collection_order=["col-1", "col-2"],
    )
    result = RecipeSync(store).sync(_FakeSyncClient([only_pasta]))

    assert result.recipes_deleted == 1
    assert result.recipes_unchanged == 1
    assert store.get_recipe("recipe-2") is None
    assert store.recipe_count() == 1
    assert store.get_meta("recipe_count") == "1"
    collections = store.list_collections()
    assert collections[0].recipe_ids == ["recipe-1"]


def test_renamed_collection_updates_memberships() -> None:
    """A renamed collection keeps its recipes."""
    store = open_store(":memory:")
    RecipeSync(store).sync(_FakeSyncClient([_two_recipe_snapshot()]))

    renamed = _recipes_snapshot(
        [_pasta_recipe(), _soup_recipe()],
        collections=[
            _collection_bytes("col-1", "Quick Dinners", ["recipe-1", "recipe-2"]),
            _collection_bytes("col-2", "Empty", []),
        ],
        collection_order=["col-1", "col-2"],
    )
    result = RecipeSync(store).sync(_FakeSyncClient([renamed]))

    assert result.collections_updated == 1
    assert result.collections_added == 0
    assert result.recipes_unchanged == 2
    collections = store.list_collections()
    assert collections[0].name == "Quick Dinners"
    assert collections[0].recipe_ids == ["recipe-1", "recipe-2"]


def test_deleted_collection_disappears() -> None:
    """A collection missing from the snapshot leaves the store."""
    store = open_store(":memory:")
    RecipeSync(store).sync(_FakeSyncClient([_two_recipe_snapshot()]))

    no_empty = _recipes_snapshot(
        [_pasta_recipe(), _soup_recipe()],
        collections=[
            _collection_bytes("col-1", "Weeknight", ["recipe-1", "recipe-2"]),
        ],
        collection_order=["col-1"],
    )
    result = RecipeSync(store).sync(_FakeSyncClient([no_empty]))

    assert result.collections_deleted == 1
    assert [c.id for c in store.list_collections()] == ["col-1"]
    assert store.get_recipe("recipe-1") is not None


def test_failed_download_leaves_store_untouched() -> None:
    """A failed download raises and changes nothing in the store."""
    store = open_store(":memory:")
    first = RecipeSync(store).sync(_FakeSyncClient([_two_recipe_snapshot()]))
    sync = RecipeSync(store)

    with pytest.raises(AnyListError):
        sync.sync(_FailingSyncClient())

    assert store.recipe_count() == 2
    assert store.get_recipe("recipe-1") is not None
    assert store.get_meta("last_sync_at") == first.synced_at
    assert store.get_meta("last_snapshot_hash") == first.snapshot_hash


def test_sync_snapshot_parses_bytes_directly() -> None:
    """sync_snapshot takes raw bytes with no client at all."""
    store = open_store(":memory:")
    result = sync_snapshot(store, _two_recipe_snapshot())
    assert result.recipes_added == 2
    assert store.recipe_count() == 2


def test_sync_recipes_takes_parsed_objects() -> None:
    """The pure diff works from parsed recipes and collections."""
    store = open_store(":memory:")
    recipes = [
        client_module.Recipe(id="r1", name="Soup", ingredients=[], preparation_steps=[]),
    ]
    collections = [
        client_module.RecipeCollection(id="c1", name="Soups", recipe_ids=["r1"])
    ]
    result = sync_recipes(store, recipes, collections)
    assert result.recipes_added == 1
    assert result.collections_added == 1
    assert store.get_recipe("r1") is not None
    assert store.get_recipe("r1").collections == ["c1"]


def test_parse_user_data_matches_getters() -> None:
    """One parsed snapshot equals the four per-section getters."""
    snapshot = (
        _shopping_user_data() + _favourites_user_data() + _two_recipe_snapshot()
    )
    client = client_module.AnyListClient(
        access_token="token",
        refresh_token="refresh",
        user_id="user-1",
        is_premium_user=True,
        client_identifier="client-1",
    )
    client.get_user_data = lambda: snapshot

    parsed = client_module.parse_user_data(snapshot)

    assert [lst.id for lst in parsed.lists] == [lst.id for lst in client.get_lists()]
    assert [item.id for item in parsed.favourites] == [
        item.id for item in client.get_favourites()
    ]
    assert [recipe.id for recipe in parsed.recipes] == [
        recipe.id for recipe in client.get_recipes()
    ]
    assert [(c.id, c.name, c.recipe_ids) for c in parsed.collections] == [
        (c.id, c.name, c.recipe_ids) for c in client.get_collections()
    ]


async def test_coordinator_fetch_downloads_once_and_syncs(
    hass: HomeAssistant,
) -> None:
    """One coordinator poll: one download, lists + favourites + a synced store."""
    snapshot = (
        _shopping_user_data() + _favourites_user_data() + _two_recipe_snapshot()
    )
    client = _FakeSyncClient([snapshot])
    store = open_store(":memory:")

    data = await _async_fetch_data_stages(hass, client, store)

    assert client.calls == 1
    assert [lst.id for lst in data["lists"]] == ["list-1"]
    assert [item.name for item in data["favourites"]] == ["Milk"]
    assert store.recipe_count() == 2
    assert store.get_recipe("recipe-1") is not None
    assert store.get_recipe("recipe-2") is not None


async def test_coordinator_fetch_without_store_skips_sync(
    hass: HomeAssistant,
) -> None:
    """Without a store the fetch still returns lists and favourites."""
    snapshot = (
        _shopping_user_data() + _favourites_user_data() + _two_recipe_snapshot()
    )
    client = _FakeSyncClient([snapshot])

    data = await _async_fetch_data_stages(hass, client, None)

    assert client.calls == 1
    assert [lst.id for lst in data["lists"]] == ["list-1"]
    assert [item.name for item in data["favourites"]] == ["Milk"]


async def test_coordinator_fetch_failure_raises_update_failed(
    hass: HomeAssistant,
) -> None:
    """A failed download fails the coordinator and leaves the store alone."""
    store = open_store(":memory:")
    RecipeSync(store).sync(_FakeSyncClient([_two_recipe_snapshot()]))

    with pytest.raises(UpdateFailed):
        await _async_fetch_data_stages(hass, _FailingSyncClient(), store)

    assert store.recipe_count() == 2
    assert store.get_recipe("recipe-1") is not None


async def test_coordinator_fetch_auth_failure(
    hass: HomeAssistant,
) -> None:
    """An auth failure during the download becomes a reauth signal."""
    from custom_components.anylist.client import AnyListAuthError

    class _AuthFailingClient:
        def get_user_data(self) -> bytes:
            raise AnyListAuthError("bad token")

    with pytest.raises(ConfigEntryAuthFailed):
        await _async_fetch_data_stages(hass, _AuthFailingClient(), None)
