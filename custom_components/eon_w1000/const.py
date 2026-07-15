"""Constants for E.ON W1000 integration."""

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
CONF_INITIAL_IMPORT = "initial_import"
CONF_INITIAL_EXPORT = "initial_export"

# Defaults
DEFAULT_IMAP_PORT = 993
DEFAULT_POLL_INTERVAL = 60  # minutes
DEFAULT_EMAIL_SENDER = "noreply@eon.com"
DEFAULT_EMAIL_SUBJECT = "[EON-W1000]"
DEFAULT_INITIAL_IMPORT = 0.0
DEFAULT_INITIAL_EXPORT = 0.0

# Sensor IDs
SENSOR_GRID_IMPORT = "grid_import"
SENSOR_GRID_EXPORT = "grid_export"

# Statistics — must match HA's auto-generated statistic_id format.
# For entity with unique_id "eon_w1000_grid_import", HA assigns:
#   statistic_id = "eon_w1000:eon_w1000_grid_import"
STATISTIC_IMPORT_ID = "eon_w1000:eon_w1000_grid_import"
STATISTIC_EXPORT_ID = "eon_w1000:eon_w1000_grid_export"

# Storage keys
STORAGE_VERSION = 1
STORAGE_KEY = "eon_w1000_processed"
