"""Unit tests for the AnyList SQLite store (``store.py``).

These tests run with no Home Assistant at all: the store module is
loaded by file path (importing the ``custom_components.anylist``
package would execute its ``__init__``, which imports Home
Assistant), and every test uses an in-memory database unless it is
specifically about a database file. Run them standalone with the
system Python (``python3 -m pytest --noconftest`` needs pytest) or
as part of the repo suite, where they run like any other test file.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

_STORE_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "anylist"
    / "store.py"
)
_CONST_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "anylist"
    / "const.py"
)


def _load_module(path: Path, name: str):
    import sys

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register before exec: @dataclass(slots=True) resolves the string
    # annotations (from __future__ import annotations) via sys.modules.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


store = _load_module(_STORE_PATH, "anylist_store_under_test")
const = _load_module(_CONST_PATH, "anylist_const_under_test")

RecipeData = store.RecipeData
IngredientData = store.IngredientData
CollectionData = store.CollectionData
RecipeStore = store.RecipeStore
StoreError = store.StoreError


def make_recipe(
    recipe_id: str = "r1",
    name: str = "Creamy Tomato Pasta",
    **overrides,
) -> RecipeData:
    """A headed recipe with verbatim quantities, steps, and a note."""
    values = {
        "id": recipe_id,
        "name": name,
        "prep_time_seconds": 600,
        "cook_time_seconds": 1200,
        "servings": "4",
        "rating": 5,
        "note": "Vihaan likes this with extra cheese.",
        "source_name": "Family",
        "source_url": "https://example.invalid/pasta",
        "photo_urls": ["https://photos.anylist.com/abc.jpg"],
        "ingredients": [
            IngredientData(kind="section", name="For the sauce"),
            IngredientData(
                kind="ingredient",
                name="Cherry tomatoes",
                quantity="400 g",
                note="halved",
                raw_ingredient="400 g Cherry tomatoes, halved",
            ),
            IngredientData(
                kind="ingredient",
                name="Cream",
                quantity="200 ml",
                raw_ingredient="200 ml Cream",
            ),
            IngredientData(kind="section", name="For the pasta"),
            IngredientData(
                kind="ingredient",
                name="Penne",
                quantity="500 g",
                raw_ingredient="500 g Penne",
            ),
            IngredientData(
                kind="ingredient",
                name="Salt",
                note="to taste",
                raw_ingredient="Salt to taste",
            ),
        ],
        "steps": [
            "Boil the pasta in salted water until al dente.",
            "# For the sauce",
            "Meanwhile, soften the tomatoes in a pan.",
            "Stir in the **cream**, add the pasta, and serve.",
        ],
        "collections": ["c-pasta"],
    }
    values.update(overrides)
    return RecipeData(**values)


@pytest.fixture()
def mem_store():
    """An open in-memory store, closed after the test."""
    with RecipeStore(":memory:").open() as store_obj:
        yield store_obj


def test_open_creates_schema_v1(mem_store):
    assert mem_store.schema_version == 1
    assert mem_store.get_meta("schema_version") == "1"
    tables = {
        row[0]
        for row in mem_store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    assert {
        "sync_meta",
        "recipes",
        "recipe_ingredients",
        "recipe_steps",
        "collections",
        "recipe_collections",
        "recipes_fts",
        "recipes_fts_data",
        "recipes_fts_idx",
        "recipes_fts_content",
        "recipes_fts_docsize",
        "recipes_fts_config",
        "vocabulary",
    } <= tables


def test_upsert_and_read_back_round_trip(mem_store):
    mem_store.upsert_collection("c-pasta", "Pasta")
    assert mem_store.upsert_recipe(make_recipe()) is True
    back = mem_store.get_recipe("r1")
    assert back is not None
    assert back.name == "Creamy Tomato Pasta"
    # Times are seconds, as AnyList stores them.
    assert back.prep_time_seconds == 600
    assert back.cook_time_seconds == 1200
    assert back.servings == "4"
    assert back.rating == 5
    assert back.note == "Vihaan likes this with extra cheese."
    assert back.source_name == "Family"
    assert back.source_url == "https://example.invalid/pasta"
    assert back.photo_urls == ["https://photos.anylist.com/abc.jpg"]
    # Headings survive in order, interleaved with the ingredients.
    assert [entry.kind for entry in back.ingredients] == [
        "section",
        "ingredient",
        "ingredient",
        "section",
        "ingredient",
        "ingredient",
    ]
    assert back.ingredients[0].name == "For the sauce"
    assert back.ingredients[3].name == "For the pasta"
    # Verbatim quantities and raw lines.
    assert back.ingredients[1].quantity == "400 g"
    assert back.ingredients[1].note == "halved"
    assert back.ingredients[1].raw_ingredient == (
        "400 g Cherry tomatoes, halved"
    )
    assert back.ingredients[5].quantity is None
    assert back.ingredients[5].raw_ingredient == "Salt to taste"
    # Steps keep their markdown verbatim.
    assert back.steps[1] == "# For the sauce"
    assert back.steps[3] == "Stir in the **cream**, add the pasta, and serve."
    assert back.collections == ["c-pasta"]
    # The normalized name is stored folded.
    row = mem_store._conn.execute(
        "SELECT name_normalized FROM recipes WHERE id = 'r1'"
    ).fetchone()
    assert row[0] == "creamy tomato pasta"


def test_upsert_unchanged_writes_nothing(mem_store):
    mem_store.upsert_collection("c-pasta", "Pasta")
    recipe = make_recipe()
    assert mem_store.upsert_recipe(recipe) is True
    before_version = mem_store._conn.execute("PRAGMA data_version").fetchone()[0]
    before_fts = mem_store._conn.execute(
        "SELECT COUNT(*) FROM recipes_fts"
    ).fetchone()[0]
    # Same content again: short-circuited, nothing rewritten.
    assert mem_store.upsert_recipe(recipe) is False
    after_version = mem_store._conn.execute("PRAGMA data_version").fetchone()[0]
    after_fts = mem_store._conn.execute(
        "SELECT COUNT(*) FROM recipes_fts"
    ).fetchone()[0]
    assert after_version == before_version
    assert after_fts == before_fts
    assert mem_store.recipe_count() == 1


def test_upsert_changed_updates_row_and_fts(mem_store):
    mem_store.upsert_collection("c-pasta", "Pasta")
    mem_store.upsert_recipe(make_recipe())
    assert mem_store.search_words("pasta")[0] == {"r1"}
    changed = make_recipe(name="Creamy Tomato Soup")
    assert mem_store.upsert_recipe(changed) is True
    assert mem_store.get_recipe("r1").name == "Creamy Tomato Soup"
    assert mem_store.search_words("pasta")[0] == set()
    assert mem_store.search_words("soup")[0] == {"r1"}


def test_delete_removes_everything(mem_store):
    mem_store.upsert_collection("c-pasta", "Pasta")
    mem_store.upsert_collection("c-soup", "Soups")
    mem_store.upsert_recipe(make_recipe("r1", "Creamy Tomato Pasta"))
    mem_store.upsert_recipe(
        make_recipe(
            "r2",
            "Carrot Ginger Soup",
            ingredients=[
                IngredientData(kind="ingredient", name="Carrots", quantity="500 g"),
                IngredientData(kind="ingredient", name="Ginger", quantity="1 tbsp"),
            ],
            steps=["Blend it."],
            collections=["c-soup"],
        )
    )
    # "carrots" is unique to r2; "creamy" is unique to r1.
    assert "carrots" in mem_store.get_vocabulary()
    assert "creamy" in mem_store.get_vocabulary()
    assert mem_store.delete_recipe("r2") is True
    assert mem_store.get_recipe("r2") is None
    assert mem_store._conn.execute(
        "SELECT COUNT(*) FROM recipe_ingredients WHERE recipe_id = 'r2'"
    ).fetchone()[0] == 0
    assert mem_store._conn.execute(
        "SELECT COUNT(*) FROM recipe_steps WHERE recipe_id = 'r2'"
    ).fetchone()[0] == 0
    assert mem_store._conn.execute(
        "SELECT COUNT(*) FROM recipe_collections WHERE recipe_id = 'r2'"
    ).fetchone()[0] == 0
    assert mem_store._conn.execute(
        "SELECT COUNT(*) FROM recipes_fts WHERE recipe_id = 'r2'"
    ).fetchone()[0] == 0
    # The unique word went with the recipe; the other recipe is untouched.
    assert "carrots" not in mem_store.get_vocabulary()
    assert "creamy" in mem_store.get_vocabulary()
    assert mem_store.search_words("carrot")[0] == set()
    assert mem_store.search_words("pasta")[0] == {"r1"}
    assert mem_store.delete_recipe("r2") is False
    assert mem_store.delete_recipe("missing") is False
    assert mem_store.get_recipe("missing") is None


def test_list_recipes_and_collections(mem_store):
    mem_store.upsert_collection("c-pasta", "Pasta")
    mem_store.upsert_collection("c-empty", "Empty collection")
    mem_store.upsert_recipe(make_recipe("r1", "Creamy Tomato Pasta"))
    mem_store.upsert_recipe(
        make_recipe(
            "r2",
            "Pesto Pasta",
            ingredients=[],
            steps=[],
            collections=["c-pasta"],
        )
    )
    recipes = mem_store.list_recipes()
    assert [recipe.id for recipe in recipes] == ["r1", "r2"]
    assert mem_store.recipe_count() == 2
    collections = mem_store.list_collections()
    assert [(c.id, c.name) for c in collections] == [
        ("c-pasta", "Pasta"),
        ("c-empty", "Empty collection"),
    ]
    # Members in stored order; empty collections are included.
    assert collections[0].recipe_ids == ["r1", "r2"]
    assert collections[1].recipe_ids == []
    # Renaming a collection is an update, re-upserting is a no-op.
    assert mem_store.upsert_collection("c-pasta", "Pasta & Noodles") is True
    assert mem_store.upsert_collection("c-pasta", "Pasta & Noodles") is False
    assert mem_store.list_collections()[0].name == "Pasta & Noodles"
    assert mem_store.delete_collection("c-empty") is True
    assert mem_store.delete_collection("c-empty") is False
    assert [c.id for c in mem_store.list_collections()] == ["c-pasta"]


def test_collection_members_keep_stored_order(mem_store):
    """A recipe in two collections must not reorder the other's members.

    The membership table's position is the position in the recipe's
    own collection list (r1 lists c-first first, c-second second),
    so ordering a collection's members by it would put r2 first.
    Members come back in stored (snapshot) order instead.
    """
    mem_store.upsert_collection("c-first", "First")
    mem_store.upsert_collection("c-second", "Second")
    mem_store.upsert_recipe(
        make_recipe("r1", "One", collections=["c-first", "c-second"])
    )
    mem_store.upsert_recipe(
        make_recipe(
            "r2",
            "Two",
            ingredients=[],
            steps=[],
            collections=["c-second"],
        )
    )
    collections = mem_store.list_collections()
    assert collections[1].recipe_ids == ["r1", "r2"]


def test_search_words_prefix_and_short_words(mem_store):
    mem_store.upsert_collection("c-pasta", "Pasta")
    mem_store.upsert_recipe(make_recipe("r1", "Creamy Tomato Pasta"))
    mem_store.upsert_recipe(
        make_recipe(
            "r2",
            "15-Minute Miso Soup",
            prep_time_seconds=300,
            cook_time_seconds=600,
            ingredients=[
                IngredientData(kind="ingredient", name="Miso paste"),
            ],
            steps=[],
            collections=[],
        )
    )
    # Trigram prefix: "carr" is not tested here (no carrot recipe), but
    # a prefix of a title word matches.
    assert mem_store.search_words("cream")[0] == {"r1"}
    assert mem_store.search_words("crea")[0] == {"r1"}
    # Words under 3 characters use the LIKE fallback.
    assert mem_store.search_words("15")[0] == {"r2"}
    # "miso" is a title word; "paste" is ingredient-only.
    assert mem_store.search_words("miso")[0] == {"r2"}
    assert mem_store.search_words("paste")[0] == set()
    assert mem_store.search_words("paste", include_ingredients=True) == (
        set(),
        {"r2"},
    )
    # Empty or multi-word input matches nothing.
    assert mem_store.search_words("") == (set(), set())
    assert mem_store.search_words("tomato pasta") == (set(), set())


def test_vocabulary_excludes_headings(mem_store):
    from dataclasses import replace

    mem_store.upsert_collection("c-pasta", "Pasta")
    recipe = make_recipe()
    recipe = replace(
        recipe,
        ingredients=[
            IngredientData(kind="section", name="For the flambé"),
            *recipe.ingredients,
        ],
    )
    mem_store.upsert_recipe(recipe)
    vocab = mem_store.get_vocabulary()
    assert "tomatoes" in vocab  # ingredient word
    assert "creamy" in vocab  # title word
    assert "flambe" not in vocab  # heading-only word stays out
    # Rebuilding the index from the tables gives the same vocabulary.
    mem_store._conn.execute("DELETE FROM vocabulary")
    mem_store.rebuild_search_index()
    assert mem_store.get_vocabulary() == vocab


def test_upsert_validates_input(mem_store):
    with pytest.raises(StoreError):
        mem_store.upsert_recipe(make_recipe(recipe_id=""))
    with pytest.raises(StoreError):
        mem_store.upsert_recipe(make_recipe(name=""))
    bad = make_recipe(
        ingredients=[IngredientData(kind="weird", name="X")]
    )
    with pytest.raises(StoreError):
        mem_store.upsert_recipe(bad)
    # A recipe referencing a collection that was never upserted fails
    # loudly instead of silently dropping the membership.
    with pytest.raises(StoreError):
        mem_store.upsert_recipe(make_recipe(collections=["c-nope"]))
    with pytest.raises(StoreError):
        mem_store.upsert_collection("", "No id")


def test_file_backed_store_survives_reopen():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "anylist" / "entry123.db"
        path.parent.mkdir()
        with RecipeStore(path).open() as store_obj:
            store_obj.upsert_collection("c-pasta", "Pasta")
            store_obj.upsert_recipe(make_recipe())
            store_obj.set_meta("last_sync_at", "2026-11-02T10:00:00Z")
        # Reopen: the data and the metadata are still there.
        with RecipeStore(path).open() as store_obj:
            assert store_obj.schema_version == 1
            assert store_obj.get_recipe("r1").name == "Creamy Tomato Pasta"
            assert store_obj.get_meta("last_sync_at") == "2026-11-02T10:00:00Z"
            assert store_obj.get_meta("missing") is None
            assert store_obj.get_meta("missing", "fallback") == "fallback"


def test_corrupt_file_is_quarantined_and_rebuilt():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "entry123.db"
        path.write_bytes(b"this is not a sqlite database at all" * 4)
        with RecipeStore(path).open() as store_obj:
            assert store_obj.schema_version == 1
            assert store_obj.recipe_count() == 0
        quarantined = list(Path(tmp).glob("entry123.corrupt-*.db"))
        assert len(quarantined) == 1
        # The quarantined file is kept for diagnosis.
        assert b"not a sqlite database" in quarantined[0].read_bytes()
        # The fresh database is a working store.
        with RecipeStore(path).open() as store_obj:
            store_obj.upsert_collection("c-pasta", "Pasta")
            assert store_obj.upsert_recipe(make_recipe()) is True


def test_newer_schema_version_is_rebuilt():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "entry123.db"
        raw = sqlite3.connect(str(path))
        raw.executescript(
            "CREATE TABLE sync_meta(key TEXT PRIMARY KEY, value TEXT);"
            "INSERT INTO sync_meta VALUES ('schema_version', '99');"
        )
        raw.commit()
        raw.close()
        with RecipeStore(path).open() as store_obj:
            assert store_obj.schema_version == 1
            assert store_obj.recipe_count() == 0
        assert len(list(Path(tmp).glob("entry123.corrupt-*.db"))) == 1


def test_normalize_matches_search_folding():
    assert store.normalize_text("Crème Brûlée!") == "creme brulee"
    assert store.normalize_text("15-Minute Miso Soup") == "15 minute miso soup"
    assert store.normalize_text("  spaced   out  ") == "spaced out"
    assert store.normalize_text("") == ""
    # The hash is content, not identity: same content, different id.
    first = make_recipe("r1")
    second = make_recipe("r2")
    assert store.compute_content_hash(first) == store.compute_content_hash(second)
    changed = make_recipe("r1", note="Different note.")
    assert store.compute_content_hash(changed) != store.compute_content_hash(first)


def test_poll_interval_constant_is_proposed_not_final():
    # 15 minutes was proposed, not confirmed: the constant must exist
    # and be clearly separate from the coordinator's live default,
    # which this feature does not change.
    assert const.ANYLIST_SQLITE_SYNC_POLL_INTERVAL == 900
    assert const.ANYLIST_DEFAULT_POLL_INTERVAL == 60
    assert const.ANYLIST_MIN_POLL_INTERVAL == 60
    assert const.ANYLIST_MAX_POLL_INTERVAL == 3600


def test_store_layer_imports_no_homeassistant():
    source = _STORE_PATH.read_text()
    assert "homeassistant" not in source
    assert "from .client" not in source
    assert "from custom_components" not in source
    assert "import sqlite3" in source
    # The sync metadata keys the sync layer will use exist.
    assert "last_sync_at" in source
    assert "recipe_count" in source
