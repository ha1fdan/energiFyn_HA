"""Constants for the Energi Fyn integration."""
from datetime import timedelta

DOMAIN = "energi_fyn"

CONF_REFRESH_TOKEN = "refresh_token"
CONF_CUSTOMER_ID = "customer_id"

# Energi Fyn publishes consumption with roughly a day of delay, so polling more
# often than a few times per day only wastes requests.
UPDATE_INTERVAL = timedelta(hours=3)
INITIAL_BACKFILL_DAYS = 30
