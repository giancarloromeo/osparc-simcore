from typing import Final

from models_library.api_schemas_directorv2.services import (
    DYNAMIC_PROXY_SERVICE_PREFIX,
    DYNAMIC_SIDECAR_SERVICE_PREFIX,
)

# dynamic services

# label storing scheduler_data to allow service
# monitoring recovery after director-v2 reboots
DYNAMIC_SIDECAR_SCHEDULER_DATA_LABEL: Final[str] = "io.simcore.scheduler-data"

# This matches registries by:
# - local
# - itisfoundation
# - 10.0.0.0:8473 (IP & Port)
DYNAMIC_SIDECAR_DOCKER_IMAGE_RE = (
    r"^(([_a-zA-Z0-9:.-]+)/)?(dynamic-sidecar):([_a-zA-Z0-9.-]+)$"
)

REGEX_DY_SERVICE_SIDECAR = rf"^{DYNAMIC_SIDECAR_SERVICE_PREFIX}_[a-zA-Z0-9-_]*"
REGEX_DY_SERVICE_PROXY = rf"^{DYNAMIC_PROXY_SERVICE_PREFIX}_[a-zA-Z0-9-_]*"

UNDEFINED_STR_METADATA = "undefined-metadata"
UNDEFINED_DOCKER_LABEL = "undefined-label"
UNDEFINED_API_BASE_URL = "https://api.local"
