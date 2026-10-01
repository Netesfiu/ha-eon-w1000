"""Constants for the E.ON W1000 integration."""

DOMAIN = "eon_w1000"
PLATFORMS = ["button", "sensor"]

# Config entry keys
CONF_IMAP_HOST = "imap_host"
CONF_IMAP_PORT = "imap_port"
CONF_IMAP_USER = "imap_user"
CONF_IMAP_PASS = "imap_pass"
CONF_POLL_INTERVAL = "poll_interval"
CONF_EMAIL_SENDER = "email_sender"
CONF_EMAIL_SUBJECT = "email_subject"
CONF_SEARCH_DAYS = "search_days"
CONF_INITIAL_IMPORT = "initial_import"
CONF_INITIAL_EXPORT = "initial_export"

# Defaults
DEFAULT_IMAP_PORT = 993
DEFAULT_POLL_INTERVAL = 60  # minutes
DEFAULT_EMAIL_SENDER = "noreply@eon.com"
DEFAULT_EMAIL_SUBJECT = "[EON-W1000]"
DEFAULT_SEARCH_DAYS = 10
DEFAULT_INITIAL_IMPORT = 0.0
DEFAULT_INITIAL_EXPORT = 0.0

# --- Statistics target -------------------------------------------------------
# The long-term statistics series that the Energy dashboard already consumes.
# For a sensor entity HA keys its statistics by the *entity_id*, and these two
# entity_ids (template helpers) are what `energy/get_prefs` points at.  Writing
# the imported hourly rows into that same series is what makes the replacement
# of the previous importer seam-free: the anchor hour is read back from the very
# series we write to.
STATISTIC_IMPORT_ID = "sensor.grid_energy_import"
STATISTIC_EXPORT_ID = "sensor.grid_energy_export"
STATISTIC_SOURCE = "recorder"

# Sensor keys (entity unique_ids, deliberately kept stable across releases)
SENSOR_GRID_IMPORT = "grid_import"
SENSOR_GRID_EXPORT = "grid_export"
SENSOR_LAST_UPDATE = "last_update"
SENSOR_LAST_PROCESSING = "last_processing"

# Storage
STORAGE_VERSION = 2
STORAGE_KEY = "eon_w1000_state"

# Mail intake
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
LEDGER_MAX_ENTRIES = 500
