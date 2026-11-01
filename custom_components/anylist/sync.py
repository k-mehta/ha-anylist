"""Recipe cache sync (sync layer).

Owns the refresh cycle: take one snapshot from the client, diff it
against the store, upsert changed rows, delete vanished rows, and
update ``sync_meta``. It knows the client's ``get_user_data()`` /
``parse_user_data()`` API and the store's upsert API. It knows nothing
about FTS internals, service responses, or Home Assistant: callers
pass a client (or snapshot bytes, or already-parsed recipes) in and
get a ``SyncResult`` back. All work here is synchronous; Home
Assistant runs it in the executor, exactly like the client calls.

Rules (design §4): the service layer never imports ``sqlite3``; the
search layer never imports the client; the storage layer never imports
Home Assistant. This module imports neither Home Assistant nor
``sqlite3``. The database is a cache, never a second source of truth:
writes still go live to AnyList, and a failed download leaves the
store untouched.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from .client import (
    Ingredient,
    Recipe,
    RecipeCollection,
    parse_user_data,
)
from .store import (
    CollectionData,
    IngredientData,
    RecipeData,
    RecipeStore,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SyncResult:
    """What one sync run did."""

    recipes_added: int = 0
    recipes_updated: int = 0
    recipes_unchanged: int = 0
    recipes_deleted: int = 0
    collections_added: int = 0
    collections_updated: int = 0
    collections_deleted: int = 0
    snapshot_hash: str | None = None
    synced_at: str | None = None

    @property
    def recipes_total(self) -> int:
        """Recipes in the store's snapshot after the sync."""
        return self.recipes_added + self.recipes_updated + self.recipes_unchanged


def _is_section(entry: object) -> bool:
    """Return whether an ingredient entry is a section heading.

    Headings are the client's ``RecipeIngredientSection`` class. The
    name is checked instead of importing the class: the headings
    support is a separate feature, so this module must import and run
    with and without it. No other entry type carries that name.
    """
    return type(entry).__name__ == "RecipeIngredientSection"


def recipe_to_data(recipe: Recipe) -> RecipeData:
    """Map a client recipe onto the store's ``RecipeData``.

    Ingredient headings come from the ordered ``ingredient_entries``
    (sections interleaved with plain ingredients, exactly as AnyList
    stores them). Recipes parsed before that field existed simply have
    none, so the mapping falls back to the flat ``ingredients`` list.
    Times are seconds already, exactly as AnyList stores them.
    Collection order is the order the client parsed them in (app order).
    """
    entries = getattr(recipe, "ingredient_entries", None) or []
    ingredients: list[IngredientData] = []
    if entries:
        for entry in entries:
            if _is_section(entry):
                ingredients.append(
                    IngredientData(kind="section", name=getattr(entry, "name", None))
                )
            elif isinstance(entry, Ingredient):
                ingredients.append(
                    IngredientData(
                        kind="ingredient",
                        name=entry.name,
                        quantity=entry.quantity,
                        note=entry.note,
                        raw_ingredient=entry.raw_ingredient,
                    )
                )
            else:  # Defensive: an unknown entry type is skipped, not stored.
                _LOGGER.warning(
                    "Skipping unknown ingredient entry type %r in recipe %r",
                    type(entry).__name__,
                    getattr(recipe, "id", None),
                )
    else:
        ingredients = [
            IngredientData(
                kind="ingredient",
                name=ingredient.name,
                quantity=ingredient.quantity,
                note=ingredient.note,
                raw_ingredient=ingredient.raw_ingredient,
            )
            for ingredient in (getattr(recipe, "ingredients", None) or [])
        ]
    return RecipeData(
        id=recipe.id,
        name=recipe.name,
        prep_time_seconds=getattr(recipe, "prep_time", None),
        cook_time_seconds=getattr(recipe, "cook_time", None),
        servings=getattr(recipe, "servings", None),
        rating=getattr(recipe, "rating", None),
        note=getattr(recipe, "note", None),
        source_name=getattr(recipe, "source_name", None),
        source_url=getattr(recipe, "source_url", None),
        photo_urls=list(getattr(recipe, "photo_urls", None) or []),
        ingredients=ingredients,
        steps=list(getattr(recipe, "preparation_steps", None) or []),
        collections=[
            collection.id
            for collection in (getattr(recipe, "collections", None) or [])
        ],
    )


def data_to_recipe(
    data: RecipeData, collections_by_id: dict[str, RecipeCollection]
) -> Recipe:
    """Map a stored recipe back onto a client ``Recipe``.

    The reverse of ``recipe_to_data``: flat ingredients only — the
    client ``Recipe`` on this branch carries no headings, so section
    rows are dropped here (the headings feature maps them into
    ``ingredient_entries`` instead). Times are seconds, exactly as
    AnyList stores them, and collections come back in the recipe's
    stored order.
    """
    return Recipe(
        id=data.id,
        name=data.name,
        ingredients=[
            Ingredient(
                name=entry.name or "",
                quantity=entry.quantity,
                note=entry.note,
                raw_ingredient=entry.raw_ingredient,
            )
            for entry in data.ingredients
            if entry.kind == "ingredient"
        ],
        preparation_steps=list(data.steps),
        note=data.note,
        source_name=data.source_name,
        source_url=data.source_url,
        servings=data.servings,
        prep_time=data.prep_time_seconds,
        cook_time=data.cook_time_seconds,
        rating=data.rating,
        photo_urls=list(data.photo_urls),
        collections=[
            collections_by_id[collection_id]
            for collection_id in data.collections
            if collection_id in collections_by_id
        ],
    )


def collection_to_data(collection: RecipeCollection) -> CollectionData:
    """Map a client collection onto the store's ``CollectionData``."""
    return CollectionData(
        id=collection.id,
        name=collection.name,
        recipe_ids=list(collection.recipe_ids),
    )


def sync_recipes(
    store: RecipeStore,
    recipes: list[Recipe],
    collections: list[RecipeCollection],
    *,
    snapshot_hash: str | None = None,
) -> SyncResult:
    """Diff parsed recipes/collections against the store and apply it.

    Order matters. Vanished recipes go first, then collections are
    upserted (recipes may only reference collections that exist), then
    recipes are upserted, and vanished collections go last — by then
    every stored recipe's memberships match the snapshot, so deleting
    a collection cannot strand a membership. Unchanged recipes are
    never rewritten (the store's content-hash short-circuit). A
    ``snapshot_hash`` (normally the sha256 of the raw snapshot bytes)
    is recorded in ``sync_meta`` when given.
    """
    synced_at = datetime.now(timezone.utc).isoformat()
    previous_recipe_ids = set(store.list_recipe_names())
    previous_collections = {
        collection.id: collection.name for collection in store.list_collections()
    }

    # Collections referenced by a recipe but missing from the snapshot's
    # collection list are still real collections (the recipe carries
    # their id and name); upsert them too so the recipe upsert below
    # never trips the store's unknown-collection check.
    effective_collections: dict[str, str] = {}
    for collection in collections:
        effective_collections[collection.id] = collection.name
    for recipe in recipes:
        for collection in getattr(recipe, "collections", None) or []:
            effective_collections.setdefault(collection.id, collection.name)

    recipes_added = 0
    recipes_updated = 0
    recipes_unchanged = 0
    recipes_deleted = 0
    collections_added = 0
    collections_updated = 0
    collections_deleted = 0

    snapshot_recipe_ids = {recipe.id for recipe in recipes}
    for recipe_id in previous_recipe_ids - snapshot_recipe_ids:
        if store.delete_recipe(recipe_id):
            recipes_deleted += 1

    for collection_id, name in effective_collections.items():
        if store.upsert_collection(collection_id, name):
            if collection_id in previous_collections:
                collections_updated += 1
            else:
                collections_added += 1

    # Membership is authoritative in the collections section: a recipe
    # belongs to every collection whose recipe_ids name it. The
    # recipe's own collections field (the client parser fills it from
    # this same section) is kept as a fallback for collections the
    # section does not list, so neither side of a hand-built input
    # loses a membership.
    memberships: dict[str, list[str]] = {}
    for collection in collections:
        for recipe_id in collection.recipe_ids:
            memberships.setdefault(recipe_id, []).append(collection.id)

    for recipe in recipes:
        data = recipe_to_data(recipe)
        extra = [
            collection.id
            for collection in getattr(recipe, "collections", None) or []
            if collection.id not in memberships.get(recipe.id, [])
        ]
        if extra or data.collections != memberships.get(recipe.id, []):
            data = replace(data, collections=memberships.get(recipe.id, []) + extra)
        if store.upsert_recipe(data):
            if recipe.id in previous_recipe_ids:
                recipes_updated += 1
            else:
                recipes_added += 1
        else:
            recipes_unchanged += 1

    for collection_id in previous_collections:
        if collection_id not in effective_collections:
            if store.delete_collection(collection_id):
                collections_deleted += 1

    recipe_count = store.recipe_count()
    store.set_meta("last_sync_at", synced_at)
    if snapshot_hash is not None:
        store.set_meta("last_snapshot_hash", snapshot_hash)
    store.set_meta("recipe_count", str(recipe_count))

    result = SyncResult(
        recipes_added=recipes_added,
        recipes_updated=recipes_updated,
        recipes_unchanged=recipes_unchanged,
        recipes_deleted=recipes_deleted,
        collections_added=collections_added,
        collections_updated=collections_updated,
        collections_deleted=collections_deleted,
        snapshot_hash=snapshot_hash,
        synced_at=synced_at,
    )
    _LOGGER.debug(
        "AnyList recipe cache sync: %d added, %d updated, %d unchanged, "
        "%d deleted recipes; %d added, %d updated, %d deleted "
        "collections; %d recipes total",
        recipes_added,
        recipes_updated,
        recipes_unchanged,
        recipes_deleted,
        collections_added,
        collections_updated,
        collections_deleted,
        recipe_count,
    )
    return result


def sync_snapshot(store: RecipeStore, snapshot: bytes) -> SyncResult:
    """Sync the store from one raw ``get_user_data()`` snapshot.

    Parses the bytes once (lists and favourites in the same snapshot
    are the coordinator's business, not the sync's) and diffs the
    recipes and collections against the store. The snapshot hash is
    the sha256 of the raw bytes.
    """
    parsed = parse_user_data(snapshot)
    return sync_recipes(
        store,
        parsed.recipes,
        parsed.collections,
        snapshot_hash=hashlib.sha256(snapshot).hexdigest(),
    )


class RecipeSync:
    """One-shot recipe sync for a store, driven by a client.

    The coordinator owns the download cadence: it fetches the snapshot
    once per poll and shares it between the list entities and this
    sync, so call ``sync_snapshot`` (or the ``sync_snapshot()``
    function) from the coordinator instead of ``sync()`` when the bytes
    are already in hand. ``sync()`` exists for the simple case: one
    ``get_user_data()`` call, then a sync. A failed download raises
    and leaves the store untouched.
    """

    def __init__(self, store: RecipeStore) -> None:
        self._store = store

    @property
    def store(self) -> RecipeStore:
        """The store this sync writes to."""
        return self._store

    def sync(self, client: Any) -> SyncResult:
        """Download one snapshot from ``client`` and sync the store."""
        return sync_snapshot(self._store, client.get_user_data())

    def sync_snapshot(self, snapshot: bytes) -> SyncResult:
        """Sync the store from snapshot bytes already in hand."""
        return sync_snapshot(self._store, snapshot)
