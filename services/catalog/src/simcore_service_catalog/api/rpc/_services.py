import functools
import logging
from typing import cast

from fastapi import FastAPI
from models_library.api_schemas_catalog.services import (
    MyServiceGet,
    PageRpcLatestServiceGet,
    PageRpcServiceRelease,
    ServiceGetV2,
    ServiceUpdateV2,
)
from models_library.products import ProductName
from models_library.rest_pagination import PageOffsetInt
from models_library.rpc_pagination import DEFAULT_NUMBER_OF_ITEMS_PER_PAGE, PageLimitInt
from models_library.services_types import ServiceKey, ServiceVersion
from models_library.users import UserID
from pydantic import ValidationError, validate_call
from pyinstrument import Profiler
from servicelib.logging_utils import log_decorator
from servicelib.rabbitmq import RPCRouter
from servicelib.rabbitmq.rpc_interfaces.catalog.errors import (
    CatalogForbiddenError,
    CatalogItemNotFoundError,
)
from simcore_service_catalog.repository.groups import GroupsRepository

from ...repository.services import ServicesRepository
from ...service import catalog_services
from .._dependencies.director import get_director_client

_logger = logging.getLogger(__name__)

router = RPCRouter()


def _profile_rpc_call(coro):
    @functools.wraps(coro)
    async def _wrapper(app: FastAPI, **kwargs):
        profile_enabled = (
            (settings := getattr(app.state, "settings", None))
            and settings.CATALOG_PROFILING
            and _logger.isEnabledFor(logging.INFO)
        )
        if profile_enabled:
            with Profiler() as profiler:
                result = await coro(app, **kwargs)
            profiler_output = profiler.output_text(unicode=True, color=False)
            _logger.info("[PROFILING]: %s", profiler_output)
            return result

        # bypasses w/o profiling
        return await coro(app, **kwargs)

    return _wrapper


@router.expose(reraise_if_error_type=(CatalogForbiddenError, ValidationError))
@_profile_rpc_call
@validate_call(config={"arbitrary_types_allowed": True})
async def list_services_paginated(
    app: FastAPI,
    *,
    product_name: ProductName,
    user_id: UserID,
    limit: PageLimitInt = DEFAULT_NUMBER_OF_ITEMS_PER_PAGE,
    offset: PageOffsetInt = 0,
) -> PageRpcLatestServiceGet:
    assert app.state.engine  # nosec

    total_count, items = await catalog_services.list_latest_catalog_services(
        repo=ServicesRepository(app.state.engine),
        director_api=get_director_client(app),
        product_name=product_name,
        user_id=user_id,
        limit=limit,
        offset=offset,
    )

    assert len(items) <= total_count  # nosec
    assert len(items) <= limit  # nosec

    return cast(
        PageRpcLatestServiceGet,
        PageRpcLatestServiceGet.create(
            items,
            total=total_count,
            limit=limit,
            offset=offset,
        ),
    )


@router.expose(
    reraise_if_error_type=(
        CatalogItemNotFoundError,
        CatalogForbiddenError,
        ValidationError,
    )
)
@log_decorator(_logger, level=logging.DEBUG)
@_profile_rpc_call
@validate_call(config={"arbitrary_types_allowed": True})
async def get_service(
    app: FastAPI,
    *,
    product_name: ProductName,
    user_id: UserID,
    service_key: ServiceKey,
    service_version: ServiceVersion,
) -> ServiceGetV2:
    assert app.state.engine  # nosec

    service = await catalog_services.get_catalog_service(
        repo=ServicesRepository(app.state.engine),
        director_api=get_director_client(app),
        product_name=product_name,
        user_id=user_id,
        service_key=service_key,
        service_version=service_version,
    )

    assert service.key == service_key  # nosec
    assert service.version == service_version  # nosec

    return service


@router.expose(
    reraise_if_error_type=(
        CatalogItemNotFoundError,
        CatalogForbiddenError,
        ValidationError,
    )
)
@log_decorator(_logger, level=logging.DEBUG)
@validate_call(config={"arbitrary_types_allowed": True})
async def update_service(
    app: FastAPI,
    *,
    product_name: ProductName,
    user_id: UserID,
    service_key: ServiceKey,
    service_version: ServiceVersion,
    update: ServiceUpdateV2,
) -> ServiceGetV2:
    """Updates editable fields of a service"""

    assert app.state.engine  # nosec

    service = await catalog_services.update_catalog_service(
        repo=ServicesRepository(app.state.engine),
        director_api=get_director_client(app),
        product_name=product_name,
        user_id=user_id,
        service_key=service_key,
        service_version=service_version,
        update=update,
    )

    assert service.key == service_key  # nosec
    assert service.version == service_version  # nosec

    return service


@router.expose(
    reraise_if_error_type=(
        CatalogItemNotFoundError,
        CatalogForbiddenError,
        ValidationError,
    )
)
@log_decorator(_logger, level=logging.DEBUG)
@validate_call(config={"arbitrary_types_allowed": True})
async def check_for_service(
    app: FastAPI,
    *,
    product_name: ProductName,
    user_id: UserID,
    service_key: ServiceKey,
    service_version: ServiceVersion,
) -> None:
    """Checks whether service exists and can be accessed, otherwise it raise"""
    assert app.state.engine  # nosec

    await catalog_services.check_catalog_service(
        repo=ServicesRepository(app.state.engine),
        product_name=product_name,
        user_id=user_id,
        service_key=service_key,
        service_version=service_version,
    )


@router.expose(reraise_if_error_type=(CatalogForbiddenError, ValidationError))
@log_decorator(_logger, level=logging.DEBUG)
@validate_call(config={"arbitrary_types_allowed": True})
async def batch_get_my_services(
    app: FastAPI,
    *,
    product_name: ProductName,
    user_id: UserID,
    ids: list[
        tuple[
            ServiceKey,
            ServiceVersion,
        ]
    ],
) -> list[MyServiceGet]:
    assert app.state.engine  # nosec

    services_batch = await catalog_services.batch_get_user_services(
        repo=ServicesRepository(app.state.engine),
        groups_repo=GroupsRepository(app.state.engine),
        product_name=product_name,
        user_id=user_id,
        ids=ids,
    )

    assert [(sv.key, sv.release.version) for sv in services_batch] == ids  # nosec

    return services_batch


@router.expose(reraise_if_error_type=(ValidationError,))
@log_decorator(_logger, level=logging.DEBUG)
@validate_call(config={"arbitrary_types_allowed": True})
async def list_my_service_history_paginated(
    app: FastAPI,
    *,
    product_name: ProductName,
    user_id: UserID,
    service_key: ServiceKey,
    limit: PageLimitInt = DEFAULT_NUMBER_OF_ITEMS_PER_PAGE,
    offset: PageOffsetInt = 0,
) -> PageRpcServiceRelease:
    assert app.state.engine  # nosec

    total_count, items = await catalog_services.list_user_service_release_history(
        repo=ServicesRepository(app.state.engine),
        product_name=product_name,
        user_id=user_id,
        service_key=service_key,
        limit=limit,
        offset=offset,
    )

    assert len(items) <= total_count  # nosec
    assert len(items) <= limit  # nosec

    return cast(
        PageRpcServiceRelease,
        PageRpcServiceRelease.create(
            items,
            total=total_count,
            limit=limit,
            offset=offset,
        ),
    )
