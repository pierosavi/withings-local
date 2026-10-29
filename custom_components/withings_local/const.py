"""Constants for the Withings Local integration."""

DOMAIN = "withings_local"
CONF_KL = "kl"
CONF_USERS = "users"  # {user id (str): first name}, from the Withings account when known
OPT_KNOWN_USERS = "known_users"  # user ids seen on the scale, in order of first appearance
GUEST_USER_ID = 0

LOCAL_NAME_PREFIX = "Body Smart"
BODY_MODEL_ID = 0x0007
BODY_SMART_MODEL_ID = 0x0010
CONF_MODEL_ID = "model_id"
SUPPORTED_MODELS = {
    BODY_MODEL_ID: ("Body", "WBS06"),
    BODY_SMART_MODEL_ID: ("Body Smart", "WBS13"),
}
# Don't reconnect for the same burst of advertisements after a sync.
POLL_COOLDOWN_SECONDS = 30
# A full read takes ~1-3 s; anything far longer is a stuck link.
SESSION_TIMEOUT_SECONDS = 45
