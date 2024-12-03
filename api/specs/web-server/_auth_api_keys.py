from typing import Annotated

from fastapi import APIRouter, Depends, status
from models_library.api_schemas_webserver.auth import ApiKeyCreate, ApiKeyGet
from models_library.generics import Envelope
from models_library.rest_error import EnvelopedError
from simcore_service_webserver._meta import API_VTAG
from simcore_service_webserver.api_keys._exceptions_handlers import _TO_HTTP_ERROR_MAP
from simcore_service_webserver.api_keys._rest import ApiKeysPathParams

router = APIRouter(
    prefix=f"/{API_VTAG}",
    tags=["auth"],
    responses={
        i.status_code: {"model": EnvelopedError} for i in _TO_HTTP_ERROR_MAP.values()
    },
)


@router.post(
    "/auth/api-keys",
    operation_id="create_api_key",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[ApiKeyGet],
)
async def create_api_key(_body: ApiKeyCreate):
    """creates API keys to access public API"""


@router.get(
    "/auth/api-keys",
    operation_id="list_display_names",
    response_model=Envelope[list[str]],
    status_code=status.HTTP_200_OK,
)
async def list_api_keys():
    """lists display names of API keys by this user"""


@router.get(
    "/auth/api-keys/{api_key_id}",
    operation_id="get_api_key",
    response_model=Envelope[ApiKeyGet],
    status_code=status.HTTP_200_OK,
)
async def get_api_key(_path: Annotated[ApiKeysPathParams, Depends()]):
    """returns the key or None"""


@router.delete(
    "/auth/api-keys",
    operation_id="delete_api_key",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_api_key(_path: Annotated[ApiKeysPathParams, Depends()]):
    """deletes API key by ID"""
