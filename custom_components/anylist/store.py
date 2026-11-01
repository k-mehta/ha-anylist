"""SQLite cache for AnyList recipes (storage layer).

Owns the SQLite connection, the schema, migrations, and every SQL
statement. It knows nothing about the AnyList API, HTTP, or Home
Assistant: callers pass plain data in (``RecipeData`` /
``CollectionData``) and get plain data out. The sync layer maps client
objects onto these dataclasses; the search layer reads through the
``search_words()`` / ``get_vocabulary()`` / ``list_recipe_names()``
read API.

Rules (design §4): the service layer never imports ``sqlite3``; the
search layer never imports the client; this module never imports Home
Assistant. The database is a cache, never a second source of truth:
writes still go live to AnyList, and losing the file costs one
re-download, nothing else.

One file per config entry, at ``config/anylist/<entry_id>.db`` (see
``open_store`` / the sync layer for the path). Times are stored as
**seconds**, exactly as AnyList stores them. Ingredient headings are
``kind="section"`` rows in the same ordered list as the ingredients.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Migrations: version number -> function migrating from version-1 to
# version. Empty today: version 1 is the first schema. When the schema
# changes, add the migration here and bump SCHEMA_VERSION; _migrate()
# applies them in order, each in its own transaction.
_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {}

_META_SCHEMA_VERSION = "schema_version"
_META_LAST_SYNC_AT = "last_sync_at"
_META_LAST_SNAPSHOT_HASH = "last_snapshot_hash"
_META_RECIPE_COUNT = "recipe_count"

_KIND_SECTION = "section"
_KIND_INGREDIENT = "ingredient"
_KINDS = (_KIND_SECTION, _KIND_INGREDIENT)

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS sync_meta(
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS recipes(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    name_normalized TEXT NOT NULL,
    prep_time_seconds INTEGER,
    cook_time_seconds INTEGER,
    servings TEXT,
    rating INTEGER,
    note TEXT,
    source_name TEXT,
    source_url TEXT,
    photo_urls TEXT,
    content_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recipe_ingredients(
    recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    kind TEXT NOT NULL,
    name TEXT,
    quantity TEXT,
    note TEXT,
    raw_ingredient TEXT,
    PRIMARY KEY (recipe_id, position)
);
CREATE TABLE IF NOT EXISTS recipe_steps(
    recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    step TEXT NOT NULL,
    PRIMARY KEY (recipe_id, position)
);
CREATE TABLE IF NOT EXISTS collections(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recipe_collections(
    recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY (recipe_id, collection_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS recipes_fts USING fts5(
    recipe_id UNINDEXED, title, ingredients, tokenize='trigram');
CREATE TABLE IF NOT EXISTS vocabulary(
    word TEXT PRIMARY KEY
);
"""


class StoreError(Exception):
    """Raised for store-level problems (bad input, missing references)."""


class IncompatibleSchemaError(StoreError):
    """The database schema is newer than this code understands."""


@dataclass(frozen=True, slots=True)
class IngredientData:
    """One ordered entry in a recipe's ingredient list."""

    kind: str  # "section" (a heading) or "ingredient"
    name: str | None = None
    quantity: str | None = None
    note: str | None = None
    raw_ingredient: str | None = None


@dataclass(frozen=True, slots=True)
class RecipeData:
    """A recipe as stored: everything AnyList has, verbatim."""

    id: str
    name: str
    prep_time_seconds: int | None = None
    cook_time_seconds: int | None = None
    servings: str | None = None
    rating: int | None = None
    note: str | None = None
    source_name: str | None = None
    source_url: str | None = None
    photo_urls: list[str] = field(default_factory=list)
    ingredients: list[IngredientData] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    # Collection ids in stored order. The collections themselves must be
    # upserted first (see upsert_collection); upsert_recipe raises
    # StoreError if one is missing.
    collections: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CollectionData:
    """A recipe collection, with its members in stored order."""

    id: str
    name: str
    recipe_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RecipeIdentity:
    """A recipe's id, name, and normalized name (for the search layer)."""

    id: str
    name: str
    name_normalized: str


def normalize_text(text: str) -> str:
    """Fold Unicode/case/accents and separate punctuation into words.

    This is the same folding as ``recipe_search.normalize_search_text``;
    the two must agree, because the store writes ``name_normalized``
    with this function and the search layer queries with that one.
    """
    text = unicodedata.normalize("NFKD", text).casefold()
    return " ".join(
        "".join(
            char if char.isalnum() else " "
            for char in text
            if not unicodedata.category(char).startswith("M")
        ).split()
    )


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so a substring search is literal."""
    return (
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def compute_content_hash(recipe: RecipeData) -> str:
    """Hash the canonical recipe fields; drives the sync diff.

    Two recipes with the same content hash are identical for sync
    purposes, so an unchanged recipe is never rewritten (keeps FTS
    churn low). The id is the row key, not part of the hash.
    """
    canonical = {
        "name": recipe.name,
        "prep_time_seconds": recipe.prep_time_seconds,
        "cook_time_seconds": recipe.cook_time_seconds,
        "servings": recipe.servings,
        "rating": recipe.rating,
        "note": recipe.note,
        "source_name": recipe.source_name,
        "source_url": recipe.source_url,
        "photo_urls": list(recipe.photo_urls),
        "ingredients": [
            {
                "kind": entry.kind,
                "name": entry.name,
                "quantity": entry.quantity,
                "note": entry.note,
                "raw_ingredient": entry.raw_ingredient,
            }
            for entry in recipe.ingredients
        ],
        "steps": list(recipe.steps),
        "collections": list(recipe.collections),
    }
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _words_of(recipe: RecipeData) -> set[str]:
    """Searchable words for a recipe: title words + ingredient words.

    Section headings are not searchable (they never appear in the
    ingredients FTS column), so heading words stay out of the
    vocabulary as well.
    """
    words = set(normalize_text(recipe.name).split())
    for entry in recipe.ingredients:
        if entry.kind == _KIND_INGREDIENT and entry.name:
            words.update(normalize_text(entry.name).split())
    return words


class RecipeStore:
    """A recipe cache backed by one SQLite file.

    All access runs under one re-entrant lock and on one connection
    (``check_same_thread=False``); callers in Home Assistant run the
    store in the executor, exactly like the client calls today.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        """The database file path (``:memory:`` for an in-memory store)."""
        return self._path

    def open(self) -> RecipeStore:
        """Connect, apply pragmas, and create/migrate the schema.

        A corrupt file (or one with a schema newer than this code) is
        quarantined next to the original as ``<name>.corrupt-<timestamp>``
        and replaced with a fresh empty database. An in-memory store has
        nothing to quarantine, so the error is re-raised there.
        """
        if self._conn is not None:
            return self
        try:
            conn = sqlite3.connect(str(self._path), check_same_thread=False)
            conn.execute("PRAGMA foreign_keys = ON")
            # WAL keeps a slow write from blocking a search. A no-op for
            # :memory: databases, which report journal_mode "memory".
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            with self._lock:
                self._ensure_schema(conn)
            self._conn = conn
        except (sqlite3.DatabaseError, IncompatibleSchemaError) as err:
            self._quarantine_and_rebuild(err)
        return self

    def close(self) -> None:
        """Close the connection. The store can be re-opened afterwards."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> RecipeStore:
        return self.open()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- schema ---------------------------------------------------------

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Create the schema on a fresh database, or migrate it."""
        has_sync_meta = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='sync_meta'"
        ).fetchone()[0]
        if not has_sync_meta:
            # Fresh database (possibly a 0-byte file): create everything.
            conn.executescript(_SCHEMA_DDL)
            self._set_meta(conn, _META_SCHEMA_VERSION, str(SCHEMA_VERSION))
            conn.commit()
            return
        current = self._schema_version(conn)
        if current is None:
            # Tables exist but no version was ever recorded: a
            # pre-versioning database. Stamp it current.
            conn.executescript(_SCHEMA_DDL)
            self._set_meta(conn, _META_SCHEMA_VERSION, str(SCHEMA_VERSION))
            conn.commit()
            return
        if current > SCHEMA_VERSION:
            raise IncompatibleSchemaError(
                f"database schema version {current} is newer than "
                f"supported version {SCHEMA_VERSION}"
            )
        self._migrate(conn, current)
        # Make sure every table exists (a partial database is repaired
        # rather than left half-built).
        conn.executescript(_SCHEMA_DDL)
        conn.commit()

    def _schema_version(self, conn: sqlite3.Connection) -> int | None:
        row = conn.execute(
            "SELECT value FROM sync_meta WHERE key = ?",
            (_META_SCHEMA_VERSION,),
        ).fetchone()
        if row is None:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return None

    def _migrate(self, conn: sqlite3.Connection, current: int) -> None:
        """Apply ordered migrations until the schema is current."""
        while current < SCHEMA_VERSION:
            nxt = current + 1
            migration = _MIGRATIONS.get(nxt)
            if migration is None:
                raise StoreError(f"no migration to schema version {nxt}")
            with conn:
                migration(conn)
                self._set_meta(conn, _META_SCHEMA_VERSION, str(nxt))
            current = nxt

    def _quarantine_and_rebuild(self, err: Exception) -> None:
        """Move a bad database file aside and start fresh.

        The quarantined file is kept for diagnosis. Raises the original
        error for in-memory stores (nothing to quarantine) and when the
        path is not a regular file.
        """
        if str(self._path) == ":memory:" or not self._path.is_file():
            raise err
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        bad_path = self._path.with_name(
            f"{self._path.stem}.corrupt-{stamp}{self._path.suffix}"
        )
        _LOGGER.warning(
            "AnyList recipe cache %s is unusable (%s); quarantining it as "
            "%s and rebuilding from a fresh database",
            self._path,
            err,
            bad_path,
        )
        self._path.rename(bad_path)
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._ensure_schema(conn)
        self._conn = conn

    # -- metadata -------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        """Read a sync_meta value (schema_version, last_sync_at, ...)."""
        conn = self._require_conn()
        with self._lock:
            row = conn.execute(
                "SELECT value FROM sync_meta WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row is not None else default

    def set_meta(self, key: str, value: str) -> None:
        """Write a sync_meta value."""
        conn = self._require_conn()
        with self._lock, conn:
            self._set_meta(conn, key, value)

    @staticmethod
    def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO sync_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    @property
    def schema_version(self) -> int | None:
        """The schema version recorded in this database."""
        return self._schema_version(self._require_conn())

    # -- recipes ----------------------------------------------------------

    def upsert_recipe(self, recipe: RecipeData) -> bool:
        """Insert a recipe, or replace it if its content changed.

        Returns True when a row was written, False when the stored
        content hash matched and nothing was rewritten. Ingredients,
        steps, collection membership, the FTS row, and the vocabulary
        are all replaced together with the recipe row, in one
        transaction.
        """
        self._validate_recipe(recipe)
        conn = self._require_conn()
        with self._lock:
            new_hash = compute_content_hash(recipe)
            row = conn.execute(
                "SELECT content_hash FROM recipes WHERE id = ?", (recipe.id,)
            ).fetchone()
            if row is not None and row[0] == new_hash:
                return False
            old_words = self._stored_words(conn, recipe.id) if row is not None else set()
            with conn:
                self._check_collections_exist(conn, recipe.collections)
                self._delete_recipe_rows(conn, recipe.id)
                conn.execute(
                    "INSERT INTO recipes(id, name, name_normalized, "
                    "prep_time_seconds, cook_time_seconds, servings, "
                    "rating, note, source_name, source_url, photo_urls, "
                    "content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        recipe.id,
                        recipe.name,
                        normalize_text(recipe.name),
                        recipe.prep_time_seconds,
                        recipe.cook_time_seconds,
                        recipe.servings,
                        recipe.rating,
                        recipe.note,
                        recipe.source_name,
                        recipe.source_url,
                        json.dumps(list(recipe.photo_urls), ensure_ascii=False),
                        new_hash,
                    ),
                )
                for position, entry in enumerate(recipe.ingredients):
                    conn.execute(
                        "INSERT INTO recipe_ingredients(recipe_id, position, "
                        "kind, name, quantity, note, raw_ingredient) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            recipe.id,
                            position,
                            entry.kind,
                            entry.name,
                            entry.quantity,
                            entry.note,
                            entry.raw_ingredient,
                        ),
                    )
                for position, step in enumerate(recipe.steps):
                    conn.execute(
                        "INSERT INTO recipe_steps(recipe_id, position, step) "
                        "VALUES (?, ?, ?)",
                        (recipe.id, position, step),
                    )
                for position, collection_id in enumerate(recipe.collections):
                    conn.execute(
                        "INSERT INTO recipe_collections(recipe_id, "
                        "collection_id, position) VALUES (?, ?, ?)",
                        (recipe.id, collection_id, position),
                    )
                conn.execute(
                    "INSERT INTO recipes_fts(recipe_id, title, ingredients) "
                    "VALUES (?, ?, ?)",
                    (
                        recipe.id,
                        normalize_text(recipe.name),
                        " ".join(
                            normalize_text(entry.name)
                            for entry in recipe.ingredients
                            if entry.kind == _KIND_INGREDIENT and entry.name
                        ),
                    ),
                )
                for word in _words_of(recipe):
                    conn.execute(
                        "INSERT OR IGNORE INTO vocabulary(word) VALUES (?)",
                        (word,),
                    )
                self._drop_unused_words(conn, old_words)
            return True

    def delete_recipe(self, recipe_id: str) -> bool:
        """Delete a recipe and everything attached to it.

        Ingredients, steps, memberships, the FTS row, and vocabulary
        words used by no remaining recipe go with it. Returns False
        when the recipe was not in the store.
        """
        conn = self._require_conn()
        with self._lock:
            row = conn.execute(
                "SELECT 1 FROM recipes WHERE id = ?", (recipe_id,)
            ).fetchone()
            if row is None:
                return False
            old_words = self._stored_words(conn, recipe_id)
            with conn:
                self._delete_recipe_rows(conn, recipe_id)
                self._drop_unused_words(conn, old_words)
            return True

    def get_recipe(self, recipe_id: str) -> RecipeData | None:
        """Read one recipe back, with headings in order, or None."""
        conn = self._require_conn()
        with self._lock:
            row = conn.execute(
                "SELECT name, prep_time_seconds, cook_time_seconds, "
                "servings, rating, note, source_name, source_url, "
                "photo_urls FROM recipes WHERE id = ?",
                (recipe_id,),
            ).fetchone()
            if row is None:
                return None
            (name, prep, cook, servings, rating, note, source_name,
             source_url, photo_urls) = row
            ingredients = [
                IngredientData(
                    kind=r[0], name=r[1], quantity=r[2], note=r[3],
                    raw_ingredient=r[4],
                )
                for r in conn.execute(
                    "SELECT kind, name, quantity, note, raw_ingredient "
                    "FROM recipe_ingredients WHERE recipe_id = ? "
                    "ORDER BY position",
                    (recipe_id,),
                )
            ]
            steps = [
                r[0]
                for r in conn.execute(
                    "SELECT step FROM recipe_steps WHERE recipe_id = ? "
                    "ORDER BY position",
                    (recipe_id,),
                )
            ]
            collections = [
                r[0]
                for r in conn.execute(
                    "SELECT collection_id FROM recipe_collections "
                    "WHERE recipe_id = ? ORDER BY position",
                    (recipe_id,),
                )
            ]
        return RecipeData(
            id=recipe_id,
            name=name,
            prep_time_seconds=prep,
            cook_time_seconds=cook,
            servings=servings,
            rating=rating,
            note=note,
            source_name=source_name,
            source_url=source_url,
            photo_urls=json.loads(photo_urls) if photo_urls else [],
            ingredients=ingredients,
            steps=steps,
            collections=collections,
        )

    def get_recipe_by_name(self, name: str) -> RecipeData | None:
        """One recipe by exact raw name, or None.

        Several recipes can normalize to the same string, so only an
        exact raw-name match counts — the old client read the same
        way, and a lowercased name simply matches nothing.
        """
        conn = self._require_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT id, name FROM recipes "
                "WHERE name_normalized = ? ORDER BY rowid",
                (normalize_text(name),),
            ).fetchall()
        for recipe_id, stored_name in rows:
            if stored_name == name:
                return self.get_recipe(recipe_id)
        return None

    def list_recipes(
        self,
        *,
        collection_id: str | None = None,
        name_substring: str | None = None,
    ) -> list[RecipeData]:
        """All recipes, in the order they were first stored.

        ``collection_id`` restricts to that collection's members, in
        stored order (matching the old per-section download).
        ``name_substring`` restricts to recipes whose normalized name
        contains the folded substring — the old get_recipes query
        filter, now a LIKE on ``name_normalized``.
        """
        conn = self._require_conn()
        with self._lock:
            conditions: list[str] = []
            params: list[Any] = []
            if collection_id is not None:
                conditions.append(
                    "id IN (SELECT recipe_id FROM recipe_collections "
                    "WHERE collection_id = ?)"
                )
                params.append(collection_id)
            if name_substring:
                conditions.append("name_normalized LIKE ? ESCAPE '\\'")
                params.append(
                    "%" + _escape_like(normalize_text(name_substring)) + "%"
                )
            where = (
                f" WHERE {' AND '.join(conditions)}" if conditions else ""
            )
            rows = conn.execute(
                "SELECT id, name, prep_time_seconds, cook_time_seconds, "
                "servings, rating, note, source_name, source_url, "
                "photo_urls FROM recipes" + where + " ORDER BY rowid",
                params,
            ).fetchall()
            if not rows:
                return []
            ids = [row[0] for row in rows]
            placeholders = ",".join("?" * len(ids))
            ingredients = conn.execute(
                "SELECT recipe_id, kind, name, quantity, note, "
                "raw_ingredient FROM recipe_ingredients "
                f"WHERE recipe_id IN ({placeholders}) "
                "ORDER BY recipe_id, position",
                ids,
            ).fetchall()
            steps = conn.execute(
                "SELECT recipe_id, step FROM recipe_steps "
                f"WHERE recipe_id IN ({placeholders}) "
                "ORDER BY recipe_id, position",
                ids,
            ).fetchall()
            memberships = conn.execute(
                "SELECT recipe_id, collection_id FROM recipe_collections "
                f"WHERE recipe_id IN ({placeholders}) "
                "ORDER BY recipe_id, position",
                ids,
            ).fetchall()
        by_recipe: dict[str, RecipeData] = {}
        for row in rows:
            (rid, name, prep, cook, servings, rating, note, source_name,
             source_url, photo_urls) = row
            by_recipe[rid] = RecipeData(
                id=rid,
                name=name,
                prep_time_seconds=prep,
                cook_time_seconds=cook,
                servings=servings,
                rating=rating,
                note=note,
                source_name=source_name,
                source_url=source_url,
                photo_urls=json.loads(photo_urls) if photo_urls else [],
            )
        for rid, kind, name, quantity, note, raw in ingredients:
            recipe = by_recipe.get(rid)
            if recipe is not None:
                recipe.ingredients.append(
                    IngredientData(
                        kind=kind, name=name, quantity=quantity, note=note,
                        raw_ingredient=raw,
                    )
                )
        for rid, step in steps:
            recipe = by_recipe.get(rid)
            if recipe is not None:
                recipe.steps.append(step)
        for rid, collection_id in memberships:
            recipe = by_recipe.get(rid)
            if recipe is not None:
                recipe.collections.append(collection_id)
        return list(by_recipe.values())

    def list_recipe_names(self) -> dict[str, RecipeIdentity]:
        """Id -> (name, normalized name) for every recipe.

        The search layer uses this for exact-title checks and sort
        order without reading full recipes.
        """
        conn = self._require_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT id, name, name_normalized FROM recipes"
            ).fetchall()
        return {
            row[0]: RecipeIdentity(id=row[0], name=row[1], name_normalized=row[2])
            for row in rows
        }

    def recipe_count(self) -> int:
        """Number of recipes in the store."""
        conn = self._require_conn()
        with self._lock:
            return conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]

    # -- collections ------------------------------------------------------

    def upsert_collection(self, collection_id: str, name: str) -> bool:
        """Insert a collection, or rename it if the name changed.

        Returns True when a row was written, False when the stored
        name already matched.
        """
        if not collection_id:
            raise StoreError("collection id must not be empty")
        conn = self._require_conn()
        with self._lock:
            row = conn.execute(
                "SELECT name FROM collections WHERE id = ?", (collection_id,)
            ).fetchone()
            if row is not None and row[0] == name:
                return False
            with conn:
                conn.execute(
                    "INSERT INTO collections(id, name) VALUES (?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET name = excluded.name",
                    (collection_id, name),
                )
            return True

    def delete_collection(self, collection_id: str) -> bool:
        """Delete a collection; its memberships go with it (cascade)."""
        conn = self._require_conn()
        with self._lock, conn:
            cur = conn.execute(
                "DELETE FROM collections WHERE id = ?", (collection_id,)
            )
            return cur.rowcount > 0

    def list_collections(self) -> list[CollectionData]:
        """All collections, including empty ones, members in order.

        Members come back in the order the recipes were stored
        (snapshot order): the membership table's ``position`` is the
        position in the *recipe's* collection list, not in the
        collection, so it cannot order members.
        """
        conn = self._require_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT id, name FROM collections ORDER BY rowid"
            ).fetchall()
            memberships = conn.execute(
                "SELECT rc.collection_id, rc.recipe_id "
                "FROM recipe_collections rc "
                "JOIN recipes r ON r.id = rc.recipe_id "
                "ORDER BY rc.collection_id, r.rowid"
            ).fetchall()
        by_id: dict[str, CollectionData] = {
            row[0]: CollectionData(id=row[0], name=row[1]) for row in rows
        }
        for collection_id, recipe_id in memberships:
            collection = by_id.get(collection_id)
            if collection is not None:
                collection.recipe_ids.append(recipe_id)
        return list(by_id.values())

    # -- search reads -------------------------------------------------------

    def search_words(
        self, word: str, *, include_ingredients: bool = False
    ) -> tuple[set[str], set[str]]:
        """Match one normalized word against title (and ingredients).

        Returns (title_hits, ingredient_hits) as recipe-id sets. Words
        of 3+ characters use the FTS5 trigram index, so a typed prefix
        matches ("carr" finds carrot recipes). Words under 3 characters
        have no trigram, so they use a word-boundary prefix LIKE
        fallback instead. An invalid (empty or multi-word) input matches
        nothing.
        """
        normalized = normalize_text(word)
        if not normalized or " " in normalized:
            return set(), set()
        conn = self._require_conn()
        with self._lock:
            if len(normalized) < 3:
                title_hits = {
                    r[0]
                    for r in conn.execute(
                        "SELECT recipe_id FROM recipes_fts "
                        "WHERE title LIKE ? OR title LIKE ?",
                        (normalized + "%", "% " + normalized + "%"),
                    )
                }
                ing_hits: set[str] = set()
                if include_ingredients:
                    ing_hits = {
                        r[0]
                        for r in conn.execute(
                            "SELECT recipe_id FROM recipes_fts "
                            "WHERE ingredients LIKE ? OR ingredients LIKE ?",
                            (normalized + "%", "% " + normalized + "%"),
                        )
                    }
                return title_hits, ing_hits
            title_hits = {
                r[0]
                for r in conn.execute(
                    "SELECT recipe_id FROM recipes_fts "
                    "WHERE recipes_fts MATCH ?",
                    (f"title:{normalized}",),
                )
            }
            ing_hits = set()
            if include_ingredients:
                ing_hits = {
                    r[0]
                    for r in conn.execute(
                        "SELECT recipe_id FROM recipes_fts "
                        "WHERE recipes_fts MATCH ?",
                        (f"ingredients:{normalized}",),
                    )
                }
            return title_hits, ing_hits

    def get_vocabulary(self) -> set[str]:
        """All distinct searchable words (title + ingredient words)."""
        conn = self._require_conn()
        with self._lock:
            return {
                r[0] for r in conn.execute("SELECT word FROM vocabulary")
            }

    def rebuild_search_index(self) -> None:
        """Rebuild the FTS rows and vocabulary from the recipe tables.

        The FTS table and vocabulary are indexes, not data: if they are
        ever wrong, this rebuilds them without a download. Used after
        migrations and as a repair path.
        """
        conn = self._require_conn()
        with self._lock, conn:
            conn.execute("DELETE FROM recipes_fts")
            conn.execute("DELETE FROM vocabulary")
            for recipe in self.list_recipes():
                conn.execute(
                    "INSERT INTO recipes_fts(recipe_id, title, ingredients) "
                    "VALUES (?, ?, ?)",
                    (
                        recipe.id,
                        normalize_text(recipe.name),
                        " ".join(
                            normalize_text(entry.name)
                            for entry in recipe.ingredients
                            if entry.kind == _KIND_INGREDIENT and entry.name
                        ),
                    ),
                )
                for word in _words_of(recipe):
                    conn.execute(
                        "INSERT OR IGNORE INTO vocabulary(word) VALUES (?)",
                        (word,),
                    )

    # -- internals ----------------------------------------------------------

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreError("store is not open; call open() first")
        return self._conn

    @staticmethod
    def _validate_recipe(recipe: RecipeData) -> None:
        if not recipe.id:
            raise StoreError("recipe id must not be empty")
        if not recipe.name:
            raise StoreError("recipe name must not be empty")
        for entry in recipe.ingredients:
            if entry.kind not in _KINDS:
                raise StoreError(
                    f"ingredient kind must be one of {_KINDS}, got {entry.kind!r}"
                )

    @staticmethod
    def _check_collections_exist(
        conn: sqlite3.Connection, collection_ids: list[str]
    ) -> None:
        for collection_id in collection_ids:
            row = conn.execute(
                "SELECT 1 FROM collections WHERE id = ?", (collection_id,)
            ).fetchone()
            if row is None:
                raise StoreError(
                    f"recipe references unknown collection {collection_id!r}; "
                    "upsert the collection first"
                )

    @staticmethod
    def _delete_recipe_rows(conn: sqlite3.Connection, recipe_id: str) -> None:
        # Children go first for clarity (the foreign keys would cascade
        # anyway); the FTS row is not a foreign-key child, so it needs
        # its own delete.
        conn.execute(
            "DELETE FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,)
        )
        conn.execute(
            "DELETE FROM recipe_steps WHERE recipe_id = ?", (recipe_id,)
        )
        conn.execute(
            "DELETE FROM recipe_collections WHERE recipe_id = ?", (recipe_id,)
        )
        conn.execute(
            "DELETE FROM recipes_fts WHERE recipe_id = ?", (recipe_id,)
        )
        conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))

    def _stored_words(self, conn: sqlite3.Connection, recipe_id: str) -> set[str]:
        """Searchable words of the stored version of a recipe."""
        row = conn.execute(
            "SELECT name FROM recipes WHERE id = ?", (recipe_id,)
        ).fetchone()
        if row is None:
            return set()
        words = set(normalize_text(row[0]).split())
        for (name,) in conn.execute(
            "SELECT name FROM recipe_ingredients "
            "WHERE recipe_id = ? AND kind = ? AND name IS NOT NULL",
            (recipe_id, _KIND_INGREDIENT),
        ):
            words.update(normalize_text(name).split())
        return words

    def _word_in_use(self, conn: sqlite3.Connection, word: str) -> bool:
        """Whether any remaining FTS row still contains this word."""
        if len(word) >= 3:
            row = conn.execute(
                "SELECT 1 FROM recipes_fts WHERE recipes_fts MATCH ? LIMIT 1",
                (word,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM recipes_fts "
                "WHERE title LIKE ? OR title LIKE ? "
                "OR ingredients LIKE ? OR ingredients LIKE ? LIMIT 1",
                (word + "%", "% " + word + "%", word + "%", "% " + word + "%"),
            ).fetchone()
        return row is not None

    def _drop_unused_words(
        self, conn: sqlite3.Connection, words: set[str]
    ) -> None:
        """Remove vocabulary words no remaining recipe uses."""
        for word in words:
            if not self._word_in_use(conn, word):
                conn.execute("DELETE FROM vocabulary WHERE word = ?", (word,))


def open_store(path: str | Path) -> RecipeStore:
    """Open the recipe store at ``path`` (``":memory:"`` for tests)."""
    return RecipeStore(path).open()
