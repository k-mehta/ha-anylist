"""Shared test helpers for the AnyList integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
import uuid

import pytest

from custom_components.anylist import client as client_module
from custom_components.anylist.client import select_recipe_collection


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading custom integrations from this repository."""


@dataclass(slots=True)
class FakeRecipe:
    """Fake AnyList recipe."""

    id: str
    name: str
    ingredients: list[Any] = field(default_factory=list)
    preparation_steps: list[str] = field(default_factory=list)
    note: str | None = None
    source_name: str | None = None
    source_url: str | None = None
    servings: str | None = None
    prep_time: int | None = None
    cook_time: int | None = None
    rating: int | None = None
    photo_urls: list[str] = field(default_factory=list)
    collections: list[Any] = field(default_factory=list)


def _snapshot_item_bytes(item: Any, list_id: str) -> bytes:
    """Serialize one fake list item to protobuf (field 4 of a list)."""
    assignment = getattr(item, "category_assignment", None)
    if assignment is not None and not (
        hasattr(assignment, "id")
        and hasattr(assignment, "category_group_id")
        and hasattr(assignment, "category_id")
    ):
        # Test doubles often carry a bare name-only assignment; the
        # real builder needs a full assignment, so drop those here.
        assignment = None
    return client_module._pb_list_item(
        item_id=item.id,
        list_id=list_id,
        name=item.name,
        user_id="user-1",
        checked=bool(getattr(item, "is_checked", False)),
        quantity=getattr(item, "quantity", None),
        details=getattr(item, "details", None) or None,
        category=getattr(item, "category", None),
        category_assignment=assignment,
        product_upc=getattr(item, "product_upc", None),
    )


def _snapshot_list_bytes(shopping_list: Any) -> bytes:
    """Serialize one fake shopping list to protobuf."""
    out = client_module._field_string(1, shopping_list.id)
    out += client_module._field_string(3, shopping_list.name)
    for item in shopping_list.items:
        out += client_module._field_message(
            4, _snapshot_item_bytes(item, shopping_list.id)
        )
    if getattr(shopping_list, "sort_order_is_set", False) or (
        getattr(shopping_list, "sort_order", "manual")
        == client_module.SORT_ORDER_ALPHABETICAL
    ):
        # The wire format cannot say "alphabetical but not stored":
        # an alphabetical list always carries its stored order.
        out += client_module._field_int32(
            17,
            1
            if shopping_list.sort_order == client_module.SORT_ORDER_ALPHABETICAL
            else 0,
        )
    if getattr(shopping_list, "new_item_position", "bottom") == (
        client_module.NEW_ITEM_POSITION_TOP
    ):
        out += client_module._field_int32(18, 1)
    return out


def _snapshot_favourite_item_bytes(item: Any) -> bytes:
    """Serialize one fake favourite item to protobuf."""
    out = client_module._field_string(1, item.id)
    out += client_module._field_string(4, item.name)
    if getattr(item, "quantity", None):
        out += client_module._field_string(18, item.quantity)
    if getattr(item, "details", None):
        out += client_module._field_string(5, item.details)
    if getattr(item, "category", None):
        out += client_module._field_string(11, item.category)
    return out


def _snapshot_favourites_bytes(favourites: list[Any]) -> bytes:
    """Serialize fake favourites as the value of top-level field 7."""
    batch = b""
    by_list: dict[str, list[Any]] = {}
    for item in favourites:
        by_list.setdefault(getattr(item, "list_id", "") or "", []).append(item)
    for list_id, items in by_list.items():
        fav_list = client_module._field_string(1, f"fav-{list_id}" or "fav")
        fav_list += client_module._field_string(3, "Favourites")
        for item in items:
            fav_list += client_module._field_message(
                4, _snapshot_favourite_item_bytes(item)
            )
        fav_list += client_module._field_string(6, list_id)
        response = client_module._field_message(1, fav_list)
        batch += client_module._field_message(1, response)
    return client_module._field_message(3, batch)


def _snapshot_recipe_bytes(recipe: Any) -> bytes:
    """Serialize one fake recipe to protobuf."""
    out = client_module._field_string(1, recipe.id)
    out += client_module._field_string(3, recipe.name)
    if getattr(recipe, "note", None):
        out += client_module._field_string(5, recipe.note)
    if getattr(recipe, "source_name", None):
        out += client_module._field_string(6, recipe.source_name)
    if getattr(recipe, "source_url", None):
        out += client_module._field_string(7, recipe.source_url)
    for ingredient in getattr(recipe, "ingredients", []) or []:
        entry = b""
        if getattr(ingredient, "raw_ingredient", None):
            entry += client_module._field_string(1, ingredient.raw_ingredient)
        entry += client_module._field_string(2, ingredient.name)
        if getattr(ingredient, "quantity", None):
            entry += client_module._field_string(3, ingredient.quantity)
        if getattr(ingredient, "note", None):
            entry += client_module._field_string(4, ingredient.note)
        out += client_module._field_message(8, entry)
    for step in getattr(recipe, "preparation_steps", []) or []:
        out += client_module._field_string(9, step)
    if getattr(recipe, "rating", None) is not None:
        out += client_module._field_int32(15, recipe.rating)
    if getattr(recipe, "cook_time", None) is not None:
        out += client_module._field_int32(18, recipe.cook_time)
    if getattr(recipe, "prep_time", None) is not None:
        out += client_module._field_int32(19, recipe.prep_time)
    if getattr(recipe, "servings", None):
        out += client_module._field_string(20, recipe.servings)
    for url in getattr(recipe, "photo_urls", []) or []:
        out += client_module._field_string(13, url)
    return out


def _snapshot_collection_bytes(collection: Any) -> bytes:
    """Serialize one fake recipe collection to protobuf."""
    out = client_module._field_string(1, collection.id)
    out += client_module._field_string(3, collection.name)
    for recipe_id in getattr(collection, "recipe_ids", []) or []:
        out += client_module._field_string(4, recipe_id)
    return out


def _snapshot_category_bytes(category: Any, list_id: str) -> bytes:
    """Serialize one fake list category to protobuf (field 5 of a group)."""
    out = client_module._field_string(1, category.id)
    if getattr(category, "category_group_id", None):
        out += client_module._field_string(3, category.category_group_id)
    if getattr(category, "list_id", None):
        out += client_module._field_string(4, category.list_id)
    out += client_module._field_string(5, category.name)
    if getattr(category, "icon", None):
        out += client_module._field_string(6, category.icon)
    if getattr(category, "match_id", None):
        out += client_module._field_string(7, category.match_id)
    out += client_module._field_int32(9, getattr(category, "sort_index", 0) or 0)
    return out


def _snapshot_assignment_bytes(assignment: Any, shopping_list: Any) -> bytes | None:
    """Serialize one fake category assignment (field 13 of category data)."""
    group_id = getattr(assignment, "category_group_id", None)
    category_id = getattr(assignment, "category_id", None)
    item_name = getattr(assignment, "item_name", None)
    if category_id is None and group_id is None and item_name is not None:
        # A bare test rule: an item name plus a category match id or
        # name. Resolve the category, then write the rule form
        # (field 6 present), with a stable synthesized id.
        match_id = getattr(assignment, "category_match_id", None)
        name = getattr(assignment, "category_name", None)
        for category in getattr(shopping_list, "categories", []) or []:
            if (match_id and getattr(category, "match_id", None) == match_id) or (
                name and category.name == name
            ):
                group_id = getattr(category, "category_group_id", None) or "group-1"
                category_id = category.id
                break
        if category_id is None:
            return None
        assignment_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"{shopping_list.id}:{item_name}"
        ).hex
        return b"".join(
            (
                client_module._field_string(1, assignment_id),
                client_module._field_string(3, shopping_list.id),
                client_module._field_string(4, group_id),
                client_module._field_string(5, item_name),
                client_module._field_string(6, category_id),
            )
        )
    if not getattr(assignment, "id", None) or not group_id or category_id is None:
        return None
    # The item name is what makes this assignment a learned rule; the
    # parser reads it from field 5 in both the rule and non-rule forms.
    out = b"".join(
        (
            client_module._field_string(1, assignment.id),
            client_module._field_string(2, group_id),
            client_module._field_string(3, category_id),
        )
    )
    if getattr(assignment, "item_name", None):
        out += client_module._field_string(5, assignment.item_name)
    return out


def _snapshot_category_data_bytes(shopping_list: Any) -> bytes | None:
    """Serialize one list's categories, sets, and assignments (field 6)."""
    categories = list(getattr(shopping_list, "categories", []) or [])
    sets = list(getattr(shopping_list, "category_sets", []) or [])
    assignments = list(getattr(shopping_list, "category_assignments", []) or [])
    if not categories and not sets and not assignments:
        return None
    # Group categories by their set; categories without a set get a
    # synthesized one so they still round-trip.
    by_group: dict[str, list[Any]] = {}
    for category in categories:
        by_group.setdefault(
            getattr(category, "category_group_id", None) or "group-1", []
        ).append(category)
    set_by_id: dict[str, Any] = {s.id: s for s in sets}
    for group_id in by_group:
        set_by_id.setdefault(
            group_id,
            SimpleNamespace(id=group_id, name="", default_category_id=None),
        )
    out = client_module._field_string(1, shopping_list.id)
    for group_id in set_by_id:
        group_set = set_by_id[group_id]
        group = client_module._field_string(1, group_id)
        group += client_module._field_string(3, shopping_list.id)
        if getattr(group_set, "name", None):
            group += client_module._field_string(4, group_set.name)
        for category in by_group.get(group_id, []):
            group += client_module._field_message(
                5, _snapshot_category_bytes(category, shopping_list.id)
            )
        if getattr(group_set, "default_category_id", None):
            group += client_module._field_string(8, group_set.default_category_id)
        container = client_module._field_message(1, group)
        out += client_module._field_message(7, container)
    for assignment in assignments:
        data = _snapshot_assignment_bytes(assignment, shopping_list)
        if data is not None:
            out += client_module._field_message(13, data)
    return out


def _snapshot_bytes(
    lists: list[Any],
    favourites: list[Any],
    recipes: list[Any],
    collections: list[Any],
) -> bytes:
    """Serialize the fake's data as one ``get_user_data()`` snapshot."""
    out = b""
    if lists:
        response = b"".join(
            client_module._field_message(1, _snapshot_list_bytes(lst)) for lst in lists
        )
        for lst in lists:
            data = _snapshot_category_data_bytes(lst)
            if data is not None:
                response += client_module._field_message(6, data)
        out += client_module._field_message(1, response)
    if favourites:
        out += client_module._field_message(7, _snapshot_favourites_bytes(favourites))
    response = b"".join(
        client_module._field_message(3, _snapshot_recipe_bytes(recipe))
        for recipe in recipes
    )
    for collection in collections:
        response += client_module._field_message(5, _snapshot_collection_bytes(collection))
    for collection in collections:
        response += client_module._field_string(4, collection.id)
    response += client_module._field_string(9, "test-recipe-data")
    out += client_module._field_message(3, response)
    return out


class FakeAnyListClient:
    """Synchronous fake matching the bundled client surface."""

    def __init__(
        self,
        *,
        user_id: str = "user-1",
        lists: list[Any] | None = None,
        favourites: list[Any] | None = None,
        recipes: list[Any] | None = None,
        collections: list[Any] | None = None,
    ) -> None:
        """Initialize the fake client."""
        self._user_id = user_id
        self.lists = lists if lists is not None else [fake_list()]
        self.favourites = favourites if favourites is not None else []
        self.recipes = recipes if recipes is not None else [fake_recipe()]
        self.collections = collections if collections is not None else []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        # Whether each added item was sent with the "insert at top" position.
        self.added_at_top: list[bool] = []

    def user_id(self) -> str:
        """Return the fake account ID."""
        return self._user_id

    def get_user_data(self) -> bytes:
        """Return the fake data as one snapshot, as the real client does."""
        self.calls.append(("get_user_data", ()))
        if getattr(self, "fail_reads", False):
            # Subclasses model an unavailable upstream with this flag;
            # the snapshot download must fail the same way get_lists does.
            raise RuntimeError("offline")
        return _snapshot_bytes(
            self.lists, self.favourites, self.recipes, self.collections
        )

    def get_lists(self) -> list[Any]:
        """Return fake shopping lists."""
        self.calls.append(("get_lists", ()))
        return self.lists

    def get_favourites(self) -> list[Any]:
        """Return fake favourites."""
        self.calls.append(("get_favourites", ()))
        return self.favourites

    def get_icalendar_info(self) -> Any:
        """Return an existing fake iCalendar URL without enabling export."""
        self.calls.append(("get_icalendar_info", ()))
        return SimpleNamespace(
            enabled=True,
            url="https://icalendar.anylist.com/redacted.ics",
            token="redacted",
        )

    def enable_icalendar(self) -> Any:
        """Return a fake iCalendar URL."""
        self.calls.append(("enable_icalendar", ()))
        return SimpleNamespace(
            enabled=True,
            url="https://icalendar.anylist.com/redacted.ics",
            token="redacted",
        )

    def get_recipes(self, collection_id=None, collection_name=None) -> list[Any]:
        """Return fake recipes."""
        if collection_id is not None or collection_name is not None:
            self.calls.append(("get_recipes", (collection_id, collection_name)))
            collection = select_recipe_collection(
                self.collections, collection_id, collection_name
            )
            return [value for value in self.recipes if value.id in collection.recipe_ids]
        self.calls.append(("get_recipes", ()))
        return self.recipes

    def get_collections(self) -> list[Any]:
        """Return fake recipe collections."""
        self.calls.append(("get_collections", ()))
        return self.collections

    def get_recipe_by_id(self, recipe_id: str) -> Any | None:
        """Return a fake recipe by ID."""
        self.calls.append(("get_recipe_by_id", (recipe_id,)))
        return next((recipe for recipe in self.recipes if recipe.id == recipe_id), None)

    def get_recipe_by_name(self, name: str) -> Any | None:
        """Return a fake recipe by name."""
        self.calls.append(("get_recipe_by_name", (name,)))
        return next((recipe for recipe in self.recipes if recipe.name == name), None)

    def add_recipe_to_list(
        self,
        recipe_id: str,
        list_id: str,
        scale_factor: float | None = None,
    ) -> None:
        """Record adding a recipe to a list."""
        self.calls.append(("add_recipe_to_list", (recipe_id, list_id, scale_factor)))

    def create_recipe(
        self,
        name: str,
        ingredients: list[Any],
        preparation_steps: list[str],
        photo_id: str | None = None,
    ) -> Any:
        """Create and return a fake recipe."""
        self.calls.append(("create_recipe", (name, ingredients, preparation_steps, photo_id)))
        recipe = FakeRecipe(
            id="created-recipe",
            name=name,
            ingredients=ingredients,
            preparation_steps=preparation_steps,
            photo_urls=[f"https://photos.anylist.com/{photo_id}.jpg"] if photo_id else [],
        )
        self.recipes.append(recipe)
        return recipe

    def upload_recipe_photo(self, image_url: str) -> str:
        """Record importing a recipe image."""
        self.calls.append(("upload_recipe_photo", (image_url,)))
        return "uploaded-photo"

    def update_recipe(
        self,
        recipe_id: str,
        name: str,
        ingredients: list[Any],
        preparation_steps: list[str],
        photo_id: str | None = None,
    ) -> None:
        """Record a fake recipe update."""
        self.calls.append(("update_recipe", (recipe_id, name, ingredients, preparation_steps, photo_id)))
        recipe = self.get_recipe_by_id(recipe_id)
        if recipe is not None:
            recipe.name = name
            recipe.ingredients = ingredients
            recipe.preparation_steps = preparation_steps
            if photo_id is not None:
                recipe.photo_urls = [f"https://photos.anylist.com/{photo_id}.jpg"]

    def delete_recipe(self, recipe_id: str) -> None:
        """Record deleting a fake recipe."""
        self.calls.append(("delete_recipe", (recipe_id,)))
        self.recipes = [recipe for recipe in self.recipes if recipe.id != recipe_id]

    def add_item(self, list_id: str, name: str, at_top: bool = False) -> Any:
        """Record adding a todo item."""
        self.calls.append(("add_item", (list_id, name)))
        self.added_at_top.append(at_top)
        item = fake_item("new-item", name)
        self.lists[0].items.insert(0 if at_top else len(self.lists[0].items), item)
        return item

    def add_item_with_details(
        self,
        list_id: str,
        name: str,
        quantity: str | None = None,
        details: str | None = None,
        category: str | None = None,
        category_assignment: Any | None = None,
        at_top: bool = False,
    ) -> Any:
        """Record adding a detailed todo item."""
        self.calls.append(
            (
                "add_item_with_details",
                (list_id, name, quantity, details, category, category_assignment),
            )
        )
        self.added_at_top.append(at_top)
        item = fake_item("new-item", name, quantity=quantity, details=details)
        self.lists[0].items.insert(0 if at_top else len(self.lists[0].items), item)
        return item

    def rename_item(
        self, list_id: str, item_id: str, name: str, original_name: str | None = None
    ) -> None:
        """Record renaming a todo item."""
        self.calls.append(("rename_item", (list_id, item_id, name, original_name)))

    def move_item(
        self, list_id: str, item_id: str, previous_item_id: str | None = None
    ) -> None:
        """Record moving a todo item."""
        self.calls.append(("move_item", (list_id, item_id, previous_item_id)))

    def set_list_sort_order(self, list_id: str, sort_order: str) -> None:
        """Record changing a list's sort order, and store it like the server."""
        self.calls.append(("set_list_sort_order", (list_id, sort_order)))
        for shopping_list in self.lists:
            if shopping_list.id == list_id:
                shopping_list.sort_order = sort_order
                shopping_list.sort_order_is_set = True

    def set_new_item_position(
        self, list_id: str, position: str, set_manual_sort_order: bool = False
    ) -> None:
        """Record changing where a list inserts new items, and store it."""
        self.calls.append(
            ("set_new_item_position", (list_id, position, set_manual_sort_order))
        )
        for shopping_list in self.lists:
            if shopping_list.id == list_id:
                shopping_list.new_item_position = position
                if set_manual_sort_order:
                    shopping_list.sort_order = client_module.SORT_ORDER_MANUAL
                    shopping_list.sort_order_is_set = True

    def cross_off_item(self, list_id: str, item_id: str) -> None:
        """Record checking off a todo item."""
        self.calls.append(("cross_off_item", (list_id, item_id)))

    def uncheck_item(self, list_id: str, item_id: str) -> None:
        """Record unchecking a todo item."""
        self.calls.append(("uncheck_item", (list_id, item_id)))

    def bulk_delete_items(self, list_id: str, item_ids: list[str]) -> None:
        """Record bulk deleting todo items."""
        self.calls.append(("bulk_delete_items", (list_id, item_ids)))


class FakeCoordinator:
    """Small coordinator stand-in for direct entity and service tests."""

    def __init__(
        self,
        data: dict[str, Any] | None = None,
        refresh_error: Exception | None = None,
    ) -> None:
        """Initialize the fake coordinator."""
        self.data = data if data is not None else {"lists": [fake_list()], "favourites": []}
        self.last_update_success = True
        self.last_exception: Exception | None = None
        self.refresh_count = 0
        self.forced_refresh_count = 0
        self.request_refresh_count = 0
        self._listeners: list[Any] = []
        self._refresh_error = refresh_error

    def async_add_listener(self, update_callback: Any) -> Any:
        """Record a listener and return an unsubscribe callback."""
        self._listeners.append(update_callback)

        def _remove_listener() -> None:
            self._listeners.remove(update_callback)

        return _remove_listener

    async def async_request_refresh(self) -> None:
        """Record a refresh request."""
        self.refresh_count += 1
        self.request_refresh_count += 1
        if self._refresh_error is not None:
            raise self._refresh_error

    async def async_refresh(self) -> None:
        """Record an immediate refresh."""
        self.refresh_count += 1
        self.forced_refresh_count += 1
        if self._refresh_error is not None:
            raise self._refresh_error


def fake_item(
    item_id: str = "item-1",
    name: str = "Milk",
    *,
    details: str = "",
    quantity: str | None = None,
    is_checked: bool = False,
    category: str | None = None,
    category_assignment: Any | None = None,
) -> Any:
    """Return a fake AnyList item."""
    return SimpleNamespace(
        id=item_id,
        list_id="list-1",
        name=name,
        details=details,
        quantity=quantity,
        is_checked=is_checked,
        category=category,
        category_assignment=category_assignment,
    )


def fake_list(
    list_id: str = "list-1",
    name: str = "Groceries",
    *,
    items: list[Any] | None = None,
    categories: list[Any] | None = None,
    category_assignments: list[Any] | None = None,
    sort_order: str = "manual",
    sort_order_is_set: bool = False,
    new_item_position: str = "bottom",
) -> Any:
    """Return a fake AnyList shopping list."""
    return SimpleNamespace(
        id=list_id,
        name=name,
        items=items if items is not None else [fake_item()],
        categories=categories if categories is not None else [],
        category_assignments=category_assignments if category_assignments is not None else [],
        sort_order=sort_order,
        sort_order_is_set=sort_order_is_set,
        new_item_position=new_item_position,
    )


def fake_recipe() -> FakeRecipe:
    """Return a fake recipe."""
    return FakeRecipe(
        id="recipe-1",
        name="Weeknight Pasta",
        ingredients=[SimpleNamespace(name="Pasta", quantity="1 box", note=None, raw_ingredient=None)],
        preparation_steps=["Boil water"],
    )
