"""The AnyList integration."""
from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.service import async_register_platform_entity_service
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import (
    AnyListAuthError,
    AnyListClient,
    AnyListCollectionError,
    AnyListError,
    AnyListHTTPError,
    AnyListTimeoutError,
    Ingredient,
    Recipe,
    RecipeCollection,
    RecipeIngredientSection,
    ShoppingList,
    async_call_with_timeout,
    parse_user_data,
    select_recipe_collection,
)
from .const import (
    ANYLIST_DEFAULT_FALLBACK_POLL_INTERVAL,
    ANYLIST_LOGIN_TIMEOUT,
    ANYLIST_MAX_FALLBACK_POLL_INTERVAL,
    ANYLIST_MIN_FALLBACK_POLL_INTERVAL,
    ANYLIST_PHOTO_TIMEOUT,
    ANYLIST_REFRESH_TIMEOUT,
    ANYLIST_REQUEST_TIMEOUT,
    ATTR_CATEGORY,
    ATTR_CATEGORY_ID,
    ATTR_CATEGORY_SET_ID,
    ATTR_COLLECTION_ID,
    ATTR_COLLECTION_NAME,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_IMAGE_URL,
    ATTR_INGREDIENTS,
    ATTR_INCLUDE_INGREDIENTS,
    ATTR_INCLUDE_COUNTS,
    ATTR_INCLUDE_STEPS,
    ATTR_ITEM,
    ATTR_LIMIT,
    ATTR_LIST_ID,
    ATTR_LIST_NAME,
    ATTR_NAME,
    ATTR_NOTE,
    ATTR_NOTES,
    ATTR_PREPARATION_STEPS,
    ATTR_QUERY,
    ATTR_QUANTITY,
    ATTR_RAW_INGREDIENT,
    ATTR_RECIPE_ID,
    ATTR_RECIPE_NAME,
    ATTR_SCALE_FACTOR,
    CONF_EMAIL,
    CONF_FALLBACK_POLL_INTERVAL,
    CONF_MEAL_PLAN_CALENDAR,
    CONF_PASSWORD,
    CONF_SELECTED_LISTS,
    DOMAIN,
    SERVICE_ADD_ITEM,
    SERVICE_ADD_RECIPE_TO_COLLECTION,
    SERVICE_ADD_RECIPE_TO_LIST,
    SERVICE_CREATE_RECIPE,
    SERVICE_CREATE_CATEGORY,
    SERVICE_DELETE_RECIPE,
    SERVICE_GET_CATEGORIES,
    SERVICE_GET_COLLECTIONS,
    SERVICE_GET_RECIPE,
    SERVICE_GET_RECIPES,
    SERVICE_REFRESH,
    SERVICE_REMOVE_RECIPE_FROM_COLLECTION,
    SERVICE_SEARCH_RECIPES,
    SERVICE_UPDATE_RECIPE,
    SERVICE_UPDATE_ITEM,
)
from .entity import account_device_info, selected_list_ids
from .recipe_search import (
    DEFAULT_SEARCH_LIMIT,
    MAX_SEARCH_LIMIT,
    normalize_search_text,
    search_store,
)
from .realtime import (
    AnyListPushListener,
    pause_coordinator_polling,
    resume_coordinator_polling,
)
from .store import RecipeStore
from .sync import (
    data_to_recipe,
    refresh_deleted_recipe,
    refresh_recipe,
    refresh_recipe_membership,
    sync_snapshot,
    updated_recipe,
)

_LOGGER = logging.getLogger(__name__)

# Base platforms always loaded
BASE_PLATFORMS: list[Platform] = [Platform.SELECT, Platform.TODO]


@dataclass(slots=True)
class AnyListRuntimeData:
    """Runtime data for a loaded AnyList config entry."""

    client: AnyListClient
    coordinator: DataUpdateCoordinator[dict[str, Any]]
    icalendar_url: str | None
    # The recipe cache (SQLite, design §5). None in tests and before
    # setup opens it; always set for a loaded entry.
    store: RecipeStore | None = None
    # The push feed (design §8 phase 2). None in tests and before setup
    # starts it; always set for a loaded entry.
    listener: AnyListPushListener | None = None


INGREDIENT_INPUT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_NAME): cv.string,
        vol.Optional(ATTR_QUANTITY): vol.Any(None, cv.string),
        vol.Optional(ATTR_NOTE): vol.Any(None, cv.string),
        vol.Optional(ATTR_RAW_INGREDIENT): vol.Any(None, cv.string),
    }
)

REFRESH_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)

GET_RECIPES_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_QUERY): cv.string,
        vol.Exclusive(ATTR_COLLECTION_ID, "collection"): vol.All(
            cv.string, vol.Length(min=1)
        ),
        vol.Exclusive(ATTR_COLLECTION_NAME, "collection"): vol.All(
            cv.string, vol.Length(min=1)
        ),
        vol.Optional(ATTR_INCLUDE_INGREDIENTS, default=True): cv.boolean,
        vol.Optional(ATTR_INCLUDE_STEPS, default=False): cv.boolean,
    }
)


def _validate_search_limit(value: Any) -> int:
    """Accept only whole numeric limits, including number-selector values."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 1 <= value <= MAX_SEARCH_LIMIT
        or value != int(value)
    ):
        raise vol.Invalid(f"Limit must be a whole number from 1 to {MAX_SEARCH_LIMIT}")
    return int(value)


SEARCH_RECIPES_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_QUERY): cv.string,
        vol.Exclusive(ATTR_COLLECTION_ID, "collection"): vol.All(
            cv.string, vol.Length(min=1)
        ),
        vol.Exclusive(ATTR_COLLECTION_NAME, "collection"): vol.All(
            cv.string, vol.Length(min=1)
        ),
        vol.Optional(ATTR_LIMIT, default=DEFAULT_SEARCH_LIMIT): _validate_search_limit,
        vol.Optional(ATTR_INCLUDE_INGREDIENTS, default=False): cv.boolean,
    }
)

GET_RECIPE_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_RECIPE_ID): cv.string,
        vol.Optional(ATTR_NAME): cv.string,
        vol.Optional(ATTR_INCLUDE_INGREDIENTS, default=True): cv.boolean,
        vol.Optional(ATTR_INCLUDE_STEPS, default=True): cv.boolean,
    }
)

GET_COLLECTIONS_SERVICE_SCHEMA = REFRESH_SERVICE_SCHEMA

RECIPE_COLLECTION_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_RECIPE_ID): vol.All(cv.string, vol.Length(min=1)),
        vol.Exclusive(ATTR_COLLECTION_ID, "collection"): vol.All(
            cv.string, vol.Length(min=1)
        ),
        vol.Exclusive(ATTR_COLLECTION_NAME, "collection"): vol.All(
            cv.string, vol.Length(min=1)
        ),
    }
)

ADD_RECIPE_TO_LIST_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_RECIPE_ID): cv.string,
        vol.Optional(ATTR_RECIPE_NAME): cv.string,
        vol.Optional(ATTR_LIST_ID): cv.string,
        vol.Optional(ATTR_LIST_NAME): cv.string,
        vol.Optional(ATTR_SCALE_FACTOR): vol.Any(None, vol.Coerce(float)),
    }
)

CREATE_RECIPE_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_NAME): cv.string,
        vol.Optional(ATTR_IMAGE_URL): cv.url,
        vol.Required(ATTR_INGREDIENTS): vol.All(
            cv.ensure_list,
            [INGREDIENT_INPUT_SCHEMA],
        ),
        vol.Required(ATTR_PREPARATION_STEPS): vol.All(
            cv.ensure_list,
            [cv.string],
        ),
    }
)

UPDATE_RECIPE_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_RECIPE_ID): cv.string,
        vol.Optional(ATTR_RECIPE_NAME): cv.string,
        vol.Required(ATTR_NAME): cv.string,
        vol.Optional(ATTR_IMAGE_URL): cv.url,
        vol.Required(ATTR_INGREDIENTS): vol.All(
            cv.ensure_list,
            [INGREDIENT_INPUT_SCHEMA],
        ),
        vol.Required(ATTR_PREPARATION_STEPS): vol.All(
            cv.ensure_list,
            [cv.string],
        ),
    }
)

DELETE_RECIPE_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_RECIPE_ID): cv.string,
        vol.Optional(ATTR_NAME): cv.string,
    }
)


def _entry_option(entry: ConfigEntry, key: str, default: Any = None) -> Any:
    """Return an option value, falling back to legacy data storage."""
    return entry.options.get(key, entry.data.get(key, default))


def get_fallback_poll_interval(entry: ConfigEntry) -> int:
    """Return the configured fallback polling interval in seconds.

    The push feed is the primary sync mechanism; this interval is only
    the safety net while the feed is down, so it is clamped to safe
    bounds. An entry that stored the old ``poll_interval`` option falls
    back to the new default: the old 60 s value is the rejected
    behaviour and is intentionally not carried over.
    """
    value = _entry_option(
        entry, CONF_FALLBACK_POLL_INTERVAL, ANYLIST_DEFAULT_FALLBACK_POLL_INTERVAL
    )
    try:
        interval = int(value)
    except (TypeError, ValueError, OverflowError):
        _LOGGER.warning(
            "Invalid AnyList fallback poll interval %r, using %s seconds",
            value,
            ANYLIST_DEFAULT_FALLBACK_POLL_INTERVAL,
        )
        return ANYLIST_DEFAULT_FALLBACK_POLL_INTERVAL

    clamped = min(
        max(interval, ANYLIST_MIN_FALLBACK_POLL_INTERVAL),
        ANYLIST_MAX_FALLBACK_POLL_INTERVAL,
    )
    if clamped != interval:
        _LOGGER.warning(
            "AnyList fallback poll interval %s seconds is out of range, using %s seconds",
            interval,
            clamped,
        )
    return clamped


def get_platforms(entry: ConfigEntry) -> list[Platform]:
    """Get platforms to load based on config."""
    platforms = list(BASE_PLATFORMS)
    if _entry_option(entry, CONF_MEAL_PLAN_CALENDAR, False):
        platforms.append(Platform.SENSOR)
    return platforms


def _translated_error(
    translation_key: str,
    **placeholders: Any,
) -> HomeAssistantError:
    """Build a translated Home Assistant service error."""
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=translation_key,
        translation_placeholders={
            key: str(value)
            for key, value in placeholders.items()
            if value is not None
        },
    )


def _is_auth_error(err: Exception) -> bool:
    """Return whether an exception should trigger reauthentication."""
    return isinstance(err, AnyListAuthError) or (
        isinstance(err, AnyListHTTPError) and err.status in {400, 401, 403}
    )


def _value_is_set(value: Any) -> bool:
    """Return whether a service value should be treated as provided."""
    return value is not None and (not isinstance(value, str) or value.strip() != "")


def _normalize_optional_string(value: Any) -> str | None:
    """Normalize an optional string value for AnyList requests."""
    if not _value_is_set(value):
        return None
    return str(value)


def _serialize_ingredient(ingredient: Any) -> dict[str, Any]:
    """Serialize an AnyList ingredient."""
    return {
        ATTR_NAME: getattr(ingredient, ATTR_NAME, None),
        ATTR_QUANTITY: getattr(ingredient, ATTR_QUANTITY, None),
        ATTR_NOTE: getattr(ingredient, ATTR_NOTE, None),
        ATTR_RAW_INGREDIENT: getattr(ingredient, ATTR_RAW_INGREDIENT, None),
    }


def _serialize_ingredient_entry(entry: Any) -> dict[str, Any]:
    """Serialize one ordered ingredient-list entry.

    Section headings serialize as `{"type": "section", "name": ...}`;
    plain ingredients serialize as `{"type": "ingredient", ...}` with the
    same four fields as the flat `ingredients` list.
    """
    if isinstance(entry, RecipeIngredientSection):
        return {"type": "section", ATTR_NAME: entry.name}
    return {"type": "ingredient", **_serialize_ingredient(entry)}


def _serialize_recipe(
    recipe: Any,
    *,
    include_ingredients: bool,
    include_steps: bool,
) -> dict[str, Any]:
    """Serialize an AnyList recipe for service responses."""
    ingredients = []
    ingredient_entries = []
    if include_ingredients:
        ingredients = [
            _serialize_ingredient(ingredient)
            for ingredient in (getattr(recipe, ATTR_INGREDIENTS, []) or [])
        ]
        # `ingredient_entries` is the ordered, heading-aware view of the
        # same list; recipes parsed before this field existed simply have
        # none, and callers fall back to `ingredients`.
        ingredient_entries = [
            _serialize_ingredient_entry(entry)
            for entry in (getattr(recipe, "ingredient_entries", []) or [])
        ]

    preparation_steps = []
    if include_steps:
        preparation_steps = list(getattr(recipe, ATTR_PREPARATION_STEPS, []) or [])

    return {
        "id": getattr(recipe, "id", None),
        ATTR_NAME: getattr(recipe, ATTR_NAME, None),
        ATTR_INGREDIENTS: ingredients,
        "ingredient_entries": ingredient_entries,
        ATTR_PREPARATION_STEPS: preparation_steps,
        ATTR_NOTE: getattr(recipe, ATTR_NOTE, None),
        "source_name": getattr(recipe, "source_name", None),
        "source_url": getattr(recipe, "source_url", None),
        "servings": getattr(recipe, "servings", None),
        "prep_time": getattr(recipe, "prep_time", None),
        "cook_time": getattr(recipe, "cook_time", None),
        "rating": getattr(recipe, "rating", None),
        "photo_urls": list(getattr(recipe, "photo_urls", []) or []),
        "collections": [
            {"id": value.id, "name": value.name}
            for value in (getattr(recipe, "collections", []) or [])
        ],
    }


def _serialize_collection(collection: Any) -> dict[str, Any]:
    """Serialize a collection with its exact recipe IDs."""
    return {
        "id": collection.id,
        "name": collection.name,
        "recipe_ids": list(collection.recipe_ids),
    }


def _require_store(
    entry_data: AnyListRuntimeData, translation_key: str
) -> RecipeStore:
    """Return the entry's recipe store, or raise the load-failed error.

    The store is None only in tests that never attached one; a loaded
    entry always has it (setup raises ConfigEntryNotReady otherwise).
    """
    store = entry_data.store
    if store is None:
        raise _translated_error(translation_key, error="recipe cache is not open")
    return store


def _collections_by_id(store: RecipeStore) -> dict[str, RecipeCollection]:
    """The store's collections as client objects, keyed by id."""
    return {
        collection.id: RecipeCollection(
            id=collection.id,
            name=collection.name,
            recipe_ids=list(collection.recipe_ids),
        )
        for collection in store.list_collections()
    }


def _resolve_collection(
    store: RecipeStore,
    collection_id: str | None,
    collection_name: str | None,
) -> RecipeCollection | None:
    """Resolve a collection selector against the store (blocking).

    Returns None when neither selector is given. Raises
    ``AnyListCollectionError`` with the same reasons as the client
    read (``collection_not_found`` / ``collection_ambiguous`` /
    ``collection_selector_required``), so the handlers translate it
    the same way.
    """
    if collection_id is None and collection_name is None:
        return None
    return select_recipe_collection(
        list(_collections_by_id(store).values()),
        collection_id,
        collection_name,
    )


def _read_recipes_from_store(
    store: RecipeStore,
    collection_id: str | None,
    collection_name: str | None,
    query: str | None,
) -> list[Recipe]:
    """Read recipes from the store as client objects (blocking).

    The collection selector is validated first, exactly as the old
    client read did; the query is the same raw substring filter the
    old handler applied, now a LIKE on the normalized name.
    """
    collection = _resolve_collection(store, collection_id, collection_name)
    by_id = _collections_by_id(store)
    data = store.list_recipes(
        collection_id=collection.id if collection is not None else None,
        name_substring=query or None,
    )
    return [data_to_recipe(recipe, by_id) for recipe in data]


def _search_store(
    store: RecipeStore,
    query: str,
    limit: int,
    include_ingredients: bool,
    collection_id: str | None,
    collection_name: str | None,
) -> dict[str, Any]:
    """Search the store, scoped to a collection when given (blocking)."""
    collection = _resolve_collection(store, collection_id, collection_name)
    return search_store(
        store,
        query,
        limit=limit,
        include_ingredients=include_ingredients,
        recipe_ids=(
            frozenset(collection.recipe_ids) if collection is not None else None
        ),
    )


def _resolve_recipe_from_store(
    store: RecipeStore,
    recipe_id: str | None,
    recipe_name: str | None,
) -> Recipe | None:
    """Resolve one recipe from the store as a client object (blocking).

    By ID, or by exact raw name. Returns None when there is no match.
    """
    if recipe_id is not None:
        data = store.get_recipe(recipe_id)
    else:
        data = store.get_recipe_by_name(recipe_name or "")
    if data is None:
        return None
    return data_to_recipe(data, _collections_by_id(store))


async def _async_resolve_recipe_from_store(
    hass: HomeAssistant,
    store: RecipeStore,
    *,
    recipe_id: str | None = None,
    recipe_name: str | None = None,
    name_label: str = ATTR_NAME,
) -> Recipe:
    """Resolve a recipe by ID or name from the store (no download).

    Same validation and errors as the client resolver, minus the
    download: exactly one selector, a missing recipe raises
    ``recipe_not_found``, a store failure raises ``recipe_load_failed``.
    The write actions keep the client resolver: they resolve live data
    at write time, and refresh the store after a successful write, so
    a later store read sees the new state.
    """
    _validate_exactly_one(
        recipe_id,
        recipe_name,
        first_label=ATTR_RECIPE_ID,
        second_label=name_label,
    )
    identifier = recipe_id if _value_is_set(recipe_id) else recipe_name
    try:
        recipe = await hass.async_add_executor_job(
            _resolve_recipe_from_store, store, recipe_id, recipe_name
        )
    except Exception as err:
        raise _translated_error(
            "recipe_load_failed",
            identifier=identifier,
            error=err,
        ) from err
    if recipe is None:
        raise _translated_error("recipe_not_found", identifier=identifier)
    return recipe


def _build_ingredients(ingredients_data: list[dict[str, Any]]) -> list[Any]:
    """Build AnyList Ingredient objects from service data."""
    return [
        Ingredient(
            name=ingredient_data[ATTR_NAME],
            quantity=_normalize_optional_string(ingredient_data.get(ATTR_QUANTITY)),
            note=_normalize_optional_string(ingredient_data.get(ATTR_NOTE)),
            raw_ingredient=_normalize_optional_string(
                ingredient_data.get(ATTR_RAW_INGREDIENT)
            ),
        )
        for ingredient_data in ingredients_data
    ]


def _get_entry_runtime_data(
    hass: HomeAssistant,
    config_entry_id: str | None,
) -> tuple[str, AnyListRuntimeData]:
    """Resolve the AnyList runtime data for a service call."""
    entries: dict[str, AnyListRuntimeData] = {
        entry.entry_id: runtime_data
        for entry in hass.config_entries.async_entries(DOMAIN)
        if (runtime_data := getattr(entry, "runtime_data", None)) is not None
    }

    if not entries:
        raise _translated_error("no_loaded_entries")

    if _value_is_set(config_entry_id):
        assert config_entry_id is not None
        if entry_data := entries.get(config_entry_id):
            return config_entry_id, entry_data

        if hass.config_entries.async_get_entry(config_entry_id) is None:
            raise _translated_error(
                "config_entry_not_found",
                config_entry_id=config_entry_id,
            )

        raise _translated_error(
            "config_entry_not_loaded",
            config_entry_id=config_entry_id,
        )

    if len(entries) == 1:
        return next(iter(entries.items()))

    raise _translated_error("multiple_entries")


def _validate_exactly_one(
    first_value: Any,
    second_value: Any,
    *,
    first_label: str,
    second_label: str,
) -> None:
    """Validate that exactly one of two service fields is set."""
    if _value_is_set(first_value) == _value_is_set(second_value):
        raise _translated_error(
            "exactly_one_field",
            first_label=first_label,
            second_label=second_label,
        )


async def _async_resolve_recipe(
    hass: HomeAssistant,
    client: Any,
    *,
    recipe_id: str | None,
    recipe_name: str | None,
    name_label: str,
) -> Any:
    """Resolve a recipe by ID or name."""
    _validate_exactly_one(
        recipe_id,
        recipe_name,
        first_label=ATTR_RECIPE_ID,
        second_label=name_label,
    )

    identifier = recipe_id if _value_is_set(recipe_id) else recipe_name

    try:
        if _value_is_set(recipe_id):
            recipe = await async_call_with_timeout(
                hass,
                client.get_recipe_by_id,
                recipe_id,
                timeout=ANYLIST_REQUEST_TIMEOUT,
            )
        else:
            recipe = await async_call_with_timeout(
                hass,
                client.get_recipe_by_name,
                recipe_name,
                timeout=ANYLIST_REQUEST_TIMEOUT,
            )
    except Exception as err:
        raise _translated_error(
            "recipe_load_failed",
            identifier=identifier,
            error=err,
        ) from err

    if recipe is None:
        raise _translated_error("recipe_not_found", identifier=identifier)

    return recipe


def _resolve_list(
    lists: Iterable[ShoppingList],
    *,
    list_id: str | None,
    list_name: str | None,
) -> ShoppingList:
    """Resolve a shopping list by ID or exact name from coordinator data.

    The coordinator already downloads the lists every poll, so
    resolving from its data costs no extra download — the old version
    called ``client.get_lists()`` on every service call. The lists
    are at most one poll old: a list renamed on the phone in the last
    minute resolves under its old name until the next poll, and the
    following live write then fails loudly if the id is gone.
    """
    _validate_exactly_one(
        list_id,
        list_name,
        first_label=ATTR_LIST_ID,
        second_label=ATTR_LIST_NAME,
    )
    for shopping_list in lists:
        if _value_is_set(list_id) and shopping_list.id == list_id:
            return shopping_list
        if _value_is_set(list_name) and shopping_list.name == list_name:
            return shopping_list
    identifier = list_id if _value_is_set(list_id) else list_name
    raise _translated_error("shopping_list_not_found", identifier=identifier)


def _open_recipe_store(hass: HomeAssistant, entry_id: str) -> RecipeStore:
    """Open the recipe store for a config entry (blocking file I/O).

    The database lives at ``config/anylist/<entry_id>.db`` (design §9);
    the directory is created if missing. Call from the executor.
    """
    path = Path(hass.config.path("anylist", f"{entry_id}.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return RecipeStore(path).open()


def _remove_recipe_store_files(db_path: Path) -> None:
    """Delete a recipe store and its SQLite sidecar files, best effort."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
        except OSError as err:
            _LOGGER.debug(
                "Could not delete AnyList recipe cache file %s: %s",
                db_path.name + suffix,
                err,
            )


async def _async_fetch_data(
    hass: HomeAssistant, client: Any, store: RecipeStore | None = None
) -> dict[str, Any]:
    """Fetch AnyList data for the coordinator."""
    _LOGGER.debug("Starting AnyList data fetch")
    try:
        return await asyncio.wait_for(
            _async_fetch_data_stages(hass, client, store),
            timeout=ANYLIST_REFRESH_TIMEOUT,
        )
    except asyncio.TimeoutError as err:
        raise UpdateFailed("Timed out fetching AnyList data") from err


async def _async_fetch_data_stages(
    hass: HomeAssistant,
    client: Any,
    store: RecipeStore | None = None,
) -> dict[str, Any]:
    """Fetch AnyList data with stage-level logging and errors.

    One ``get_user_data()`` download per poll feeds the lists, the
    favourites, and the recipe-cache sync (design §6) — the snapshot
    bytes are parsed once and shared, instead of one download per
    section as before.
    """
    try:
        _LOGGER.debug("Fetching AnyList data")
        data = await async_call_with_timeout(
            hass,
            client.get_user_data,
            timeout=ANYLIST_REQUEST_TIMEOUT,
        )
        snapshot = parse_user_data(data)
    except AnyListTimeoutError as err:
        raise UpdateFailed("Timed out fetching AnyList data") from err
    except Exception as err:
        if _is_auth_error(err):
            raise ConfigEntryAuthFailed("AnyList authentication failed") from err
        raise UpdateFailed(f"Error fetching AnyList data: {err}") from err

    _LOGGER.debug(
        "Fetched %s AnyList list(s), %s favourite(s), %s recipe(s)",
        len(snapshot.lists),
        len(snapshot.favourites),
        len(snapshot.recipes),
    )

    if store is not None:
        try:
            result = await hass.async_add_executor_job(sync_snapshot, store, data)
            _LOGGER.debug(
                "AnyList recipe cache sync: %d added, %d updated, "
                "%d unchanged, %d deleted recipes",
                result.recipes_added,
                result.recipes_updated,
                result.recipes_unchanged,
                result.recipes_deleted,
            )
        except Exception as err:
            # The cache is a cache: a failed sync must not take the
            # shopping lists down with it. The last good data keeps
            # serving until the next poll.
            _LOGGER.warning("AnyList recipe cache sync failed: %s", err)

    _LOGGER.debug("AnyList data fetch complete")
    return {
        "lists": snapshot.lists,
        "favourites": snapshot.favourites,
    }


async def _async_refresh_entry(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """Refresh coordinator data for a loaded AnyList config entry."""
    coordinator = _get_entry_runtime_data(hass, entry_id)[1].coordinator
    _LOGGER.debug("AnyList refresh started for config entry %s", entry_id)
    try:
        await asyncio.wait_for(
            coordinator.async_refresh(),
            timeout=ANYLIST_REFRESH_TIMEOUT,
        )
    except asyncio.TimeoutError as err:
        _LOGGER.warning("AnyList refresh timed out for config entry %s", entry_id)
        raise _translated_error("refresh_timed_out") from err
    except Exception as err:
        _LOGGER.warning(
            "AnyList refresh failed for config entry %s: %s",
            entry_id,
            err,
        )
        if isinstance(err, HomeAssistantError):
            raise
        raise _translated_error("refresh_failed", error=err) from err

    if not coordinator.last_update_success:
        error = coordinator.last_exception or UpdateFailed(
            "AnyList coordinator refresh failed without an exception"
        )
        _LOGGER.warning(
            "AnyList refresh failed for config entry %s: %s",
            entry_id,
            error,
        )
        raise _translated_error("refresh_failed", error=error) from error

    _LOGGER.debug("AnyList refresh succeeded for config entry %s", entry_id)
    return coordinator.data


async def _async_refresh_store_after_write(
    hass: HomeAssistant,
    store: RecipeStore | None,
    *,
    recipe: Recipe | None = None,
    recipe_id: str | None = None,
    membership_recipe_id: str | None = None,
    membership_collection: RecipeCollection | None = None,
    membership_add: bool = False,
) -> None:
    """Best-effort cache refresh after a successful recipe write.

    Upserts the given recipe, deletes the given id, or updates one
    recipe's collection memberships — no downloads. A refresh failure
    is logged, never raised: the write already succeeded, and the
    next poll repairs the cache. A missing store (tests, or an entry
    set up before the cache existed) is a no-op.
    """
    if store is None:
        return
    try:
        if recipe is not None:
            await hass.async_add_executor_job(refresh_recipe, store, recipe)
        elif recipe_id is not None:
            await hass.async_add_executor_job(
                refresh_deleted_recipe, store, recipe_id
            )
        elif (
            membership_recipe_id is not None
            and membership_collection is not None
        ):
            await hass.async_add_executor_job(
                refresh_recipe_membership,
                store,
                membership_recipe_id,
                membership_collection,
                membership_add,
            )
    except Exception as err:
        _LOGGER.warning(
            "AnyList recipe cache refresh after write failed: %s", err
        )


def _async_register_services(hass: HomeAssistant) -> None:
    """Register AnyList services."""
    if hass.services.has_service(DOMAIN, SERVICE_GET_RECIPES):
        return

    async def async_handle_refresh(call: ServiceCall) -> None:
        """Refresh AnyList coordinator data."""
        entry_id, _ = _get_entry_runtime_data(
            hass,
            call.data.get(ATTR_CONFIG_ENTRY_ID),
        )
        _LOGGER.debug("Refreshing AnyList data for config entry %s", entry_id)
        await _async_refresh_entry(hass, entry_id)

    async def async_handle_get_recipes(call: ServiceCall) -> ServiceResponse:
        """Return AnyList recipes for automations, from the local cache."""
        _, entry_data = _get_entry_runtime_data(
            hass,
            call.data.get(ATTR_CONFIG_ENTRY_ID),
        )
        store = _require_store(entry_data, "recipes_load_failed")
        try:
            recipes = await hass.async_add_executor_job(
                _read_recipes_from_store,
                store,
                call.data.get(ATTR_COLLECTION_ID),
                call.data.get(ATTR_COLLECTION_NAME),
                call.data.get(ATTR_QUERY),
            )
        except AnyListCollectionError as err:
            raise _translated_error(err.reason) from err
        except Exception as err:
            raise _translated_error("recipes_load_failed", error=err) from err
        include_ingredients = call.data[ATTR_INCLUDE_INGREDIENTS]
        include_steps = call.data[ATTR_INCLUDE_STEPS]
        return {
            "recipes": [
                _serialize_recipe(
                    recipe,
                    include_ingredients=include_ingredients,
                    include_steps=include_steps,
                )
                for recipe in recipes
            ]
        }

    async def async_handle_search_recipes(call: ServiceCall) -> ServiceResponse:
        """Search the local cache and return candidates for a get_recipe call."""
        query = call.data[ATTR_QUERY]
        if not normalize_search_text(query):
            raise _translated_error("empty_search_query")
        _, entry_data = _get_entry_runtime_data(
            hass, call.data.get(ATTR_CONFIG_ENTRY_ID)
        )
        store = _require_store(entry_data, "recipes_load_failed")
        try:
            return await hass.async_add_executor_job(
                _search_store,
                store,
                query,
                call.data[ATTR_LIMIT],
                call.data[ATTR_INCLUDE_INGREDIENTS],
                call.data.get(ATTR_COLLECTION_ID),
                call.data.get(ATTR_COLLECTION_NAME),
            )
        except AnyListCollectionError as err:
            raise _translated_error(err.reason) from err
        except Exception as err:
            raise _translated_error("recipes_load_failed", error=err) from err

    async def async_handle_get_recipe(call: ServiceCall) -> ServiceResponse:
        """Return a single AnyList recipe, from the local cache."""
        # Request validation first: a missing selector is the caller's
        # error, whatever state the cache is in.
        _validate_exactly_one(
            call.data.get(ATTR_RECIPE_ID),
            call.data.get(ATTR_NAME),
            first_label=ATTR_RECIPE_ID,
            second_label=ATTR_NAME,
        )
        _, entry_data = _get_entry_runtime_data(
            hass,
            call.data.get(ATTR_CONFIG_ENTRY_ID),
        )
        store = _require_store(entry_data, "recipe_load_failed")
        recipe = await _async_resolve_recipe_from_store(
            hass,
            store,
            recipe_id=call.data.get(ATTR_RECIPE_ID),
            recipe_name=call.data.get(ATTR_NAME),
        )

        return {
            "recipe": _serialize_recipe(
                recipe,
                include_ingredients=call.data[ATTR_INCLUDE_INGREDIENTS],
                include_steps=call.data[ATTR_INCLUDE_STEPS],
            )
        }

    async def async_handle_get_collections(call: ServiceCall) -> ServiceResponse:
        """Return user collections with their recipe IDs, including empty ones."""
        _, entry_data = _get_entry_runtime_data(
            hass, call.data.get(ATTR_CONFIG_ENTRY_ID)
        )
        store = _require_store(entry_data, "collections_load_failed")
        try:
            collections = await hass.async_add_executor_job(store.list_collections)
        except Exception as err:
            raise _translated_error("collections_load_failed", error=err) from err
        return {"collections": [_serialize_collection(value) for value in collections]}

    async def async_handle_recipe_collection(call: ServiceCall) -> ServiceResponse | None:
        """Add or remove one membership and return the confirmed collection."""
        _validate_exactly_one(
            call.data.get(ATTR_COLLECTION_ID),
            call.data.get(ATTR_COLLECTION_NAME),
            first_label=ATTR_COLLECTION_ID,
            second_label=ATTR_COLLECTION_NAME,
        )
        _, entry_data = _get_entry_runtime_data(
            hass, call.data.get(ATTR_CONFIG_ENTRY_ID)
        )
        client = entry_data.client
        change = (
            client.add_recipe_to_collection
            if call.service == SERVICE_ADD_RECIPE_TO_COLLECTION
            else client.remove_recipe_from_collection
        )
        try:
            collection = await async_call_with_timeout(
                hass,
                change,
                call.data[ATTR_RECIPE_ID],
                call.data.get(ATTR_COLLECTION_ID),
                call.data.get(ATTR_COLLECTION_NAME),
                timeout=ANYLIST_REFRESH_TIMEOUT,
            )
        except AnyListCollectionError as err:
            raise _translated_error(err.reason) from err
        except Exception as err:
            raise _translated_error("collection_update_failed", error=err) from err
        await _async_refresh_store_after_write(
            hass,
            entry_data.store,
            membership_recipe_id=call.data[ATTR_RECIPE_ID],
            membership_collection=collection,
            membership_add=call.service == SERVICE_ADD_RECIPE_TO_COLLECTION,
        )
        if not call.return_response:
            return None
        return {"collection": _serialize_collection(collection)}

    async def async_handle_add_recipe_to_list(
        call: ServiceCall,
    ) -> ServiceResponse | None:
        """Add a recipe to a shopping list."""
        entry_id, entry_data = _get_entry_runtime_data(
            hass,
            call.data.get(ATTR_CONFIG_ENTRY_ID),
        )
        client = entry_data.client
        recipe = await _async_resolve_recipe(
            hass,
            client,
            recipe_id=call.data.get(ATTR_RECIPE_ID),
            recipe_name=call.data.get(ATTR_RECIPE_NAME),
            name_label=ATTR_RECIPE_NAME,
        )
        shopping_list = _resolve_list(
            (entry_data.coordinator.data or {}).get("lists", []),
            list_id=call.data.get(ATTR_LIST_ID),
            list_name=call.data.get(ATTR_LIST_NAME),
        )

        _LOGGER.debug(
            "Adding recipe '%s' to AnyList shopping list '%s'",
            getattr(recipe, ATTR_NAME, recipe.id),
            shopping_list.name,
        )

        try:
            await async_call_with_timeout(
                hass,
                client.add_recipe_to_list,
                recipe.id,
                shopping_list.id,
                call.data.get(ATTR_SCALE_FACTOR),
                timeout=ANYLIST_REFRESH_TIMEOUT,
            )
        except Exception as err:
            raise _translated_error(
                "add_recipe_to_list_failed",
                recipe=getattr(recipe, ATTR_NAME, recipe.id),
                shopping_list=shopping_list.name,
                error=err,
            ) from err

        await _async_refresh_entry(hass, entry_id)

        if not call.return_response:
            return None

        return {
            "recipe_id": recipe.id,
            "recipe_name": getattr(recipe, ATTR_NAME, None),
            "list_id": shopping_list.id,
            "list_name": shopping_list.name,
            ATTR_SCALE_FACTOR: call.data.get(ATTR_SCALE_FACTOR),
        }

    async def async_handle_create_recipe(call: ServiceCall) -> ServiceResponse | None:
        """Create an AnyList recipe."""
        _, entry_data = _get_entry_runtime_data(
            hass,
            call.data.get(ATTR_CONFIG_ENTRY_ID),
        )
        client = entry_data.client
        ingredients = _build_ingredients(call.data[ATTR_INGREDIENTS])
        preparation_steps = call.data[ATTR_PREPARATION_STEPS]

        _LOGGER.debug("Creating AnyList recipe '%s'", call.data[ATTR_NAME])

        try:
            photo_id = None
            if image_url := call.data.get(ATTR_IMAGE_URL):
                photo_id = await async_call_with_timeout(
                    hass,
                    client.upload_recipe_photo,
                    image_url,
                    timeout=ANYLIST_PHOTO_TIMEOUT,
                )
            recipe = await async_call_with_timeout(
                hass,
                client.create_recipe,
                call.data[ATTR_NAME],
                ingredients,
                preparation_steps,
                photo_id,
                timeout=ANYLIST_REFRESH_TIMEOUT,
            )
        except Exception as err:
            raise _translated_error(
                "create_recipe_failed",
                recipe=call.data[ATTR_NAME],
                error=err,
            ) from err

        await _async_refresh_store_after_write(
            hass, entry_data.store, recipe=recipe
        )

        if not call.return_response:
            return None

        return {
            "recipe": _serialize_recipe(
                recipe,
                include_ingredients=True,
                include_steps=True,
            )
        }

    async def async_handle_update_recipe(call: ServiceCall) -> ServiceResponse | None:
        """Update an AnyList recipe."""
        _, entry_data = _get_entry_runtime_data(
            hass,
            call.data.get(ATTR_CONFIG_ENTRY_ID),
        )
        client = entry_data.client
        recipe = await _async_resolve_recipe(
            hass,
            client,
            recipe_id=call.data.get(ATTR_RECIPE_ID),
            recipe_name=call.data.get(ATTR_RECIPE_NAME),
            name_label=ATTR_RECIPE_NAME,
        )
        ingredients = _build_ingredients(call.data[ATTR_INGREDIENTS])
        preparation_steps = call.data[ATTR_PREPARATION_STEPS]

        _LOGGER.debug(
            "Updating AnyList recipe '%s'",
            getattr(recipe, ATTR_NAME, recipe.id),
        )

        try:
            photo_id = None
            if image_url := call.data.get(ATTR_IMAGE_URL):
                photo_id = await async_call_with_timeout(
                    hass,
                    client.upload_recipe_photo,
                    image_url,
                    timeout=ANYLIST_PHOTO_TIMEOUT,
                )
            await async_call_with_timeout(
                hass,
                client.update_recipe,
                recipe.id,
                call.data[ATTR_NAME],
                ingredients,
                preparation_steps,
                photo_id,
                timeout=ANYLIST_REFRESH_TIMEOUT,
            )
        except Exception as err:
            raise _translated_error(
                "update_recipe_failed",
                recipe=getattr(recipe, ATTR_NAME, recipe.id),
                error=err,
            ) from err

        await _async_refresh_store_after_write(
            hass,
            entry_data.store,
            recipe=updated_recipe(
                recipe,
                name=call.data[ATTR_NAME],
                ingredients=ingredients,
                preparation_steps=preparation_steps,
                photo_id=photo_id,
            ),
        )

        if not call.return_response:
            return None

        resolved_recipe = await _async_resolve_recipe(
            hass,
            client,
            recipe_id=recipe.id,
            recipe_name=None,
            name_label=ATTR_RECIPE_NAME,
        )
        return {
            "recipe": _serialize_recipe(
                resolved_recipe,
                include_ingredients=True,
                include_steps=True,
            )
        }

    async def async_handle_delete_recipe(call: ServiceCall) -> ServiceResponse | None:
        """Delete an AnyList recipe."""
        _, entry_data = _get_entry_runtime_data(
            hass,
            call.data.get(ATTR_CONFIG_ENTRY_ID),
        )
        client = entry_data.client
        recipe = await _async_resolve_recipe(
            hass,
            client,
            recipe_id=call.data.get(ATTR_RECIPE_ID),
            recipe_name=call.data.get(ATTR_NAME),
            name_label=ATTR_NAME,
        )

        _LOGGER.debug(
            "Deleting AnyList recipe '%s'",
            getattr(recipe, ATTR_NAME, recipe.id),
        )

        try:
            await async_call_with_timeout(
                hass,
                client.delete_recipe,
                recipe.id,
                timeout=ANYLIST_REFRESH_TIMEOUT,
            )
        except Exception as err:
            raise _translated_error(
                "delete_recipe_failed",
                recipe=getattr(recipe, ATTR_NAME, recipe.id),
                error=err,
            ) from err

        await _async_refresh_store_after_write(
            hass, entry_data.store, recipe_id=recipe.id
        )

        if not call.return_response:
            return None

        return {
            "recipe_id": recipe.id,
            "recipe_name": getattr(recipe, ATTR_NAME, None),
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH,
        async_handle_refresh,
        schema=REFRESH_SERVICE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_RECIPES,
        async_handle_get_recipes,
        schema=GET_RECIPES_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEARCH_RECIPES,
        async_handle_search_recipes,
        schema=SEARCH_RECIPES_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_RECIPE,
        async_handle_get_recipe,
        schema=GET_RECIPE_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_COLLECTIONS,
        async_handle_get_collections,
        schema=GET_COLLECTIONS_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    for service in (
        SERVICE_ADD_RECIPE_TO_COLLECTION,
        SERVICE_REMOVE_RECIPE_FROM_COLLECTION,
    ):
        hass.services.async_register(
            DOMAIN,
            service,
            async_handle_recipe_collection,
            schema=RECIPE_COLLECTION_SERVICE_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_RECIPE_TO_LIST,
        async_handle_add_recipe_to_list,
        schema=ADD_RECIPE_TO_LIST_SERVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CREATE_RECIPE,
        async_handle_create_recipe,
        schema=CREATE_RECIPE_SERVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_RECIPE,
        async_handle_update_recipe,
        schema=UPDATE_RECIPE_SERVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_RECIPE,
        async_handle_delete_recipe,
        schema=DELETE_RECIPE_SERVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the AnyList integration."""
    _async_register_services(hass)
    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_ADD_ITEM,
        entity_domain=Platform.TODO,
        schema={
            vol.Required(ATTR_NAME): cv.string,
            vol.Optional(ATTR_QUANTITY): cv.string,
            vol.Optional(ATTR_NOTES): cv.string,
            vol.Exclusive(ATTR_CATEGORY, "category_selector"): cv.string,
            vol.Exclusive(ATTR_CATEGORY_ID, "category_selector"): cv.string,
            vol.Optional(ATTR_CATEGORY_SET_ID): cv.string,
        },
        func="async_add_item",
    )
    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_UPDATE_ITEM,
        entity_domain=Platform.TODO,
        schema={
            vol.Required(ATTR_ITEM): cv.string,
            vol.Optional(ATTR_QUANTITY): cv.string,
            vol.Optional(ATTR_NOTES): cv.string,
            vol.Exclusive(ATTR_CATEGORY, "category_selector"): cv.string,
            vol.Exclusive(ATTR_CATEGORY_ID, "category_selector"): cv.string,
            vol.Optional(ATTR_CATEGORY_SET_ID): cv.string,
        },
        func="async_update_item",
    )
    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_GET_CATEGORIES,
        entity_domain=Platform.TODO,
        schema={
            vol.Optional(ATTR_CATEGORY_SET_ID): cv.string,
            vol.Optional(ATTR_INCLUDE_COUNTS, default=False): cv.boolean,
        },
        func="async_get_categories",
        supports_response=SupportsResponse.ONLY,
    )
    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_CREATE_CATEGORY,
        entity_domain=Platform.TODO,
        schema={
            vol.Required(ATTR_NAME): cv.string,
            vol.Optional(ATTR_CATEGORY_SET_ID): cv.string,
        },
        func="async_create_category",
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def _async_update_listener(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Reload AnyList when options are changed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old AnyList config entries."""
    _LOGGER.debug(
        "Migrating AnyList config entry from version %s.%s",
        entry.version,
        entry.minor_version,
    )

    if entry.version > 1:
        return False

    data = dict(entry.data)
    options = dict(entry.options)

    if entry.minor_version < 2:
        for key in (CONF_SELECTED_LISTS, CONF_MEAL_PLAN_CALENDAR):
            if key in data and key not in options:
                options[key] = data.pop(key)

        hass.config_entries.async_update_entry(
            entry,
            data=data,
            options=options,
            version=1,
            minor_version=2,
        )

    _LOGGER.debug("Migration to AnyList config entry version 1.2 successful")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up AnyList from a config entry."""
    email = entry.data[CONF_EMAIL]
    password = entry.data[CONF_PASSWORD]

    try:
        client = await async_call_with_timeout(
            hass,
            AnyListClient.login,
            email,
            password,
            timeout=ANYLIST_LOGIN_TIMEOUT,
        )
    except Exception as err:
        if _is_auth_error(err):
            raise ConfigEntryAuthFailed("AnyList authentication failed") from err

        if isinstance(err, AnyListError):
            raise ConfigEntryNotReady(f"Failed to connect to AnyList: {err}") from err

        raise ConfigEntryNotReady(f"Unexpected AnyList setup failure: {err}") from err

    # The recipe cache opens here so the coordinator's first refresh can
    # populate it; a corrupt cache file is quarantined and rebuilt by
    # the store itself (design §9). If opening fails, setup retries
    # rather than running without a cache.
    try:
        store = await hass.async_add_executor_job(
            _open_recipe_store, hass, entry.entry_id
        )
    except Exception as err:
        raise ConfigEntryNotReady(f"Failed to open AnyList recipe cache: {err}") from err

    icalendar_url = None
    if _entry_option(entry, CONF_MEAL_PLAN_CALENDAR, False):
        try:
            info = await async_call_with_timeout(
                hass,
                client.get_icalendar_info,
                timeout=ANYLIST_REQUEST_TIMEOUT,
            )
            if not info.enabled:
                info = await async_call_with_timeout(
                    hass,
                    client.enable_icalendar,
                    timeout=ANYLIST_REQUEST_TIMEOUT,
                )
            icalendar_url = info.url
            if icalendar_url:
                _LOGGER.debug("AnyList meal plan calendar URL retrieved")
            else:
                _LOGGER.warning("Failed to get iCalendar URL from AnyList")
        except Exception as err:
            _LOGGER.warning("Failed to get iCalendar URL: %s", err)

    async def async_update_data() -> dict[str, Any]:
        """Fetch data from AnyList."""
        return await _async_fetch_data(hass, client, store)

    # The coordinator's interval is only the fallback safety net: the
    # push feed below pauses polling entirely while it is connected
    # (design §8 phase 2). It starts as the fallback so the entry still
    # syncs if the feed cannot connect.
    fallback_interval = get_fallback_poll_interval(entry)
    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=DOMAIN,
        update_method=async_update_data,
        update_interval=timedelta(seconds=fallback_interval),
        always_update=True,
    )

    await coordinator.async_config_entry_first_refresh()

    runtime_data = AnyListRuntimeData(
        client=client,
        coordinator=coordinator,
        icalendar_url=icalendar_url,
        store=store,
    )
    entry.runtime_data = runtime_data
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Shopping list devices are reached through the account device.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **account_device_info(entry)
    )
    _async_remove_stale_devices(hass, entry, coordinator.data.get("lists", []))

    platforms = get_platforms(entry)
    await hass.config_entries.async_forward_entry_setups(entry, platforms)

    # Start the push feed last, after the first refresh has populated the
    # store. While it is connected, polling stops entirely; while it is
    # down, the fallback interval above is the safety net. The feed is
    # best-effort: if it cannot connect, setup still succeeds and the
    # entry keeps syncing on the fallback poll.
    async def _on_feed_connected() -> None:
        pause_coordinator_polling(coordinator)

    async def _on_feed_disconnected() -> None:
        resume_coordinator_polling(coordinator, fallback_interval)
        await coordinator.async_request_refresh()

    listener = AnyListPushListener(
        hass,
        client,
        on_sync=coordinator.async_request_refresh,
        on_connected=_on_feed_connected,
        on_disconnected=_on_feed_disconnected,
    )
    runtime_data.listener = listener
    listener.start()
    _LOGGER.debug(
        "AnyList push feed started for config entry %s; fallback poll every %s seconds while it is down",
        entry.entry_id,
        fallback_interval,
    )

    return True


def _list_id_from_device(entry: ConfigEntry, device: dr.DeviceEntry) -> str | None:
    """Return the shopping list ID a device stands for, if it is a list device."""
    prefix = f"{entry.entry_id}_"
    for domain, identifier in device.identifiers:
        if domain == DOMAIN and identifier.startswith(prefix):
            return identifier.removeprefix(prefix)
    return None


def _async_remove_stale_devices(
    hass: HomeAssistant, entry: ConfigEntry, lists: list[Any]
) -> None:
    """Remove devices of lists that were deselected or no longer exist."""
    selected_lists = selected_list_ids(entry)
    current_list_ids = {shopping_list.id for shopping_list in lists}
    device_registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        list_id = _list_id_from_device(entry, device)
        if list_id is None:
            continue
        deselected = bool(selected_lists) and list_id not in selected_lists
        # An empty response is not trusted as proof that every list was deleted.
        deleted = bool(current_list_ids) and list_id not in current_list_ids
        if deselected or deleted:
            device_registry.async_remove_device(device.id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow removing the device of a list that is no longer provided."""
    list_id = _list_id_from_device(entry, device)
    if list_id is None:
        return False

    runtime_data = getattr(entry, "runtime_data", None)
    lists = (
        (runtime_data.coordinator.data or {}).get("lists", [])
        if runtime_data is not None
        else []
    )
    selected_lists = selected_list_ids(entry)
    return not any(
        shopping_list.id == list_id
        and (not selected_lists or list_id in selected_lists)
        for shopping_list in lists
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    platforms = get_platforms(entry)
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, platforms):
        runtime_data = getattr(entry, "runtime_data", None)
        if runtime_data is not None:
            # Stop the push feed first so no more refreshes are triggered
            # while the entry is coming down.
            if runtime_data.listener is not None:
                await runtime_data.listener.stop()
            if runtime_data.store is not None:
                runtime_data.store.close()
        entry.runtime_data = None

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the recipe cache when the config entry is removed."""
    db_path = Path(hass.config.path("anylist", f"{entry.entry_id}.db"))
    await hass.async_add_executor_job(_remove_recipe_store_files, db_path)
