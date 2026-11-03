"""Constants for the AnyList integration."""

DOMAIN = "anylist"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_MEAL_PLAN_CALENDAR = "meal_plan_calendar"
CONF_SELECTED_LISTS = "selected_lists"
CONF_FALLBACK_POLL_INTERVAL = "fallback_poll_interval"

# Data keys
DATA_CLIENT = "client"
DATA_COORDINATOR = "coordinator"
DATA_ICALENDAR_URL = "icalendar_url"

# Update intervals
ANYLIST_REQUEST_TIMEOUT = 15  # seconds
ANYLIST_LOGIN_TIMEOUT = 20  # seconds
ANYLIST_REFRESH_TIMEOUT = 30  # seconds
ANYLIST_PHOTO_TIMEOUT = 65  # upload, token refresh, and image processing

# Fallback safety-net poll (design §8 phase 2). The push feed is the
# primary sync mechanism; the coordinator polls ONLY while the feed is
# down. The interval is a user option, never a hard-coded cadence:
# Kshitij rejected polling as the mechanism — no polling at night, no
# fixed interval doing downloads forever. Default 60 minutes is
# PROPOSED, not confirmed — do not treat it as final.
# The old poll_interval option (60 s default) is intentionally NOT
# carried over: its stored value is the rejected behaviour, so entries
# that stored it fall back to the new default instead.
ANYLIST_DEFAULT_FALLBACK_POLL_INTERVAL = 3600  # seconds (60 minutes, proposed)
ANYLIST_MIN_FALLBACK_POLL_INTERVAL = 300  # seconds (5 minutes)
ANYLIST_MAX_FALLBACK_POLL_INTERVAL = 3600  # seconds (60 minutes)

# Services
SERVICE_REFRESH = "refresh"
SERVICE_ADD_ITEM = "add_item"
SERVICE_UPDATE_ITEM = "update_item"
SERVICE_GET_CATEGORIES = "get_categories"
SERVICE_CREATE_CATEGORY = "create_category"
SERVICE_GET_RECIPES = "get_recipes"
SERVICE_SEARCH_RECIPES = "search_recipes"
SERVICE_GET_RECIPE = "get_recipe"
SERVICE_GET_COLLECTIONS = "get_collections"
SERVICE_ADD_RECIPE_TO_COLLECTION = "add_recipe_to_collection"
SERVICE_REMOVE_RECIPE_FROM_COLLECTION = "remove_recipe_from_collection"
SERVICE_ADD_RECIPE_TO_LIST = "add_recipe_to_list"
SERVICE_CREATE_RECIPE = "create_recipe"
SERVICE_UPDATE_RECIPE = "update_recipe"
SERVICE_DELETE_RECIPE = "delete_recipe"

# Service attributes
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_QUERY = "query"
ATTR_LIMIT = "limit"
ATTR_INCLUDE_INGREDIENTS = "include_ingredients"
ATTR_INCLUDE_STEPS = "include_steps"
ATTR_RECIPE_ID = "recipe_id"
ATTR_RECIPE_NAME = "recipe_name"
ATTR_COLLECTION_ID = "collection_id"
ATTR_COLLECTION_NAME = "collection_name"
ATTR_LIST_ID = "list_id"
ATTR_LIST_NAME = "list_name"
ATTR_NAME = "name"
ATTR_ITEM = "item"
ATTR_CATEGORY = "category"
ATTR_CATEGORY_ID = "category_id"
ATTR_CATEGORY_SET_ID = "category_set_id"
ATTR_INCLUDE_COUNTS = "include_counts"
ATTR_NOTES = "notes"
ATTR_IMAGE_URL = "image_url"
ATTR_INGREDIENTS = "ingredients"
ATTR_PREPARATION_STEPS = "preparation_steps"
ATTR_SCALE_FACTOR = "scale_factor"
ATTR_QUANTITY = "quantity"
ATTR_NOTE = "note"
ATTR_RATING = "rating"
ATTR_RAW_INGREDIENT = "raw_ingredient"
