# pylint: disable=no-value-for-parameter
# FIXME: E1120:No value for argument 'dml' in method call
# pylint: disable=protected-access
# FIXME: Access to a protected member _result_proxy of a client class

import asyncio
import logging
import os
import re
import tempfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import aiobotocore
import attr
import sqlalchemy as sa
from aiohttp import web
from aiopg.sa import Engine
from blackfynn.base import UnauthorizedException
from s3wrapper.s3_client import S3Client
from servicelib.aiopg_utils import DBAPIError, PostgresRetryPolicyUponOperation
from servicelib.client_session import get_client_session
from servicelib.utils import fire_and_forget_task
from tenacity import retry
from yarl import URL

from .access_layer import (
    AccessRights,
    get_file_access_rights,
    get_project_access_rights,
    get_readable_project_ids,
)
from .datcore_wrapper import DatcoreWrapper
from .models import (
    DatasetMetaData,
    FileMetaData,
    FileMetaDataEx,
    file_meta_data,
    get_location_from_id,
    projects,
)
from .s3 import get_config_s3
from .settings import (
    APP_CONFIG_KEY,
    APP_DB_ENGINE_KEY,
    APP_DSM_KEY,
    APP_S3_KEY,
    DATCORE_ID,
    DATCORE_STR,
    SIMCORE_S3_ID,
    SIMCORE_S3_STR,
)
from .utils import download_to_file_or_raise, expo

logger = logging.getLogger(__name__)

postgres_service_retry_policy_kwargs = PostgresRetryPolicyUponOperation(logger).kwargs


def setup_dsm(app: web.Application):
    async def _cleanup_context(app: web.Application):
        cfg = app[APP_CONFIG_KEY]

        main_cfg = cfg

        engine = app.get(APP_DB_ENGINE_KEY)
        loop = asyncio.get_event_loop()
        s3_client = app.get(APP_S3_KEY)

        max_workers = main_cfg["max_workers"]
        pool = ThreadPoolExecutor(max_workers=max_workers)

        s3_cfg = get_config_s3(app)
        bucket_name = s3_cfg["bucket_name"]

        testing = main_cfg["testing"]
        dsm = DataStorageManager(
            s3_client, engine, loop, pool, bucket_name, not testing, app
        )

        app[APP_DSM_KEY] = dsm

        yield

        # NOTE: write here clean up

    app.cleanup_ctx.append(_cleanup_context)


@attr.s(auto_attribs=True)
class DatCoreApiToken:
    api_token: Optional[str] = None
    api_secret: Optional[str] = None

    def to_tuple(self):
        return (self.api_token, self.api_secret)


@attr.s(auto_attribs=True)
class DataStorageManager:
    """Data storage manager

    The dsm has access to the database for all meta data and to the actual backend. For now this
    is simcore's S3 [minio] and the datcore storage facilities.

    For all data that is in-house (simcore.s3, ...) we keep a synchronized database with meta information
    for the physical files.

    For physical changes on S3, that might be time-consuming, the db keeps a state (delete and upload mostly)

    The dsm provides the following additional functionalities:

    - listing of folders for a given users, optionally filtered using a regular expression and optionally
      sorted by one of the meta data keys

    - upload/download of files

        client -> S3 : presigned upload link
        S3 -> client : presigned download link
        datcore -> client: presigned download link
        S3 -> datcore: local copy and then upload via their api

    minio/S3 and postgres can talk nicely with each other via Notifications using rabbigMQ which we already have.
    See:

        https://blog.minio.io/part-5-5-publish-minio-events-via-postgresql-50f6cc7a7346
        https://docs.minio.io/docs/minio-bucket-notification-guide.html
    """

    s3_client: S3Client
    engine: Engine
    loop: object
    pool: ThreadPoolExecutor
    simcore_bucket_name: str
    has_project_db: bool
    app: Optional[web.Application] = None

    datcore_tokens: Dict[str, DatCoreApiToken] = attr.Factory(dict)
    # TODO: perhaps can be used a cache? add a lifetime?

    def _get_datcore_tokens(self, user_id: str) -> Tuple[str, str]:
        # pylint: disable=no-member
        token = self.datcore_tokens.get(user_id, DatCoreApiToken())
        return token.to_tuple()

    async def locations(self, user_id: str):
        locs = []
        simcore_s3 = {"name": SIMCORE_S3_STR, "id": SIMCORE_S3_ID}
        locs.append(simcore_s3)

        ping_ok = await self.ping_datcore(user_id=user_id)
        if ping_ok:
            datcore = {"name": DATCORE_STR, "id": DATCORE_ID}
            locs.append(datcore)

        return locs

    @classmethod
    def location_from_id(cls, location_id: str):
        return get_location_from_id(location_id)

    async def ping_datcore(self, user_id: str) -> bool:
        """Checks whether user account in datcore is accesible

        :param user_id: user identifier
        :type user_id: str
        :return: True if user can access his datcore account
        :rtype: bool
        """

        api_token, api_secret = self._get_datcore_tokens(user_id)
        logger.info("token: %s, secret %s", api_token, api_secret)
        if api_token:
            try:
                dcw = DatcoreWrapper(api_token, api_secret, self.loop, self.pool)
                profile = await dcw.ping()
                if profile:
                    return True
            except UnauthorizedException:
                logger.exception("Connection to datcore not possible")

        return False

    # LIST/GET ---------------------------

    # pylint: disable=too-many-arguments
    # pylint: disable=too-many-branches
    # pylint: disable=too-many-statements
    async def list_files(
        self, user_id: str, location: str, uuid_filter: str = "", regex: str = ""
    ) -> List[FileMetaDataEx]:
        """Returns a list of file paths

        - Works for simcore.s3 and datcore
        - Can filter on uuid: useful to filter on project_id/node_id
        - Can filter upon regular expression (for now only on key: value pairs of the FileMetaData)
        """
        data = deque()
        if location == SIMCORE_S3_STR:
            accesible_projects_ids = []
            async with self.engine.acquire() as conn:
                async with conn.begin():
                    accesible_projects_ids = await get_readable_project_ids(
                        conn, int(user_id)
                    )
                    has_read_access = (
                        file_meta_data.c.user_id == user_id
                    ) | file_meta_data.c.project_id.in_(accesible_projects_ids)

                    query = sa.select([file_meta_data]).where(has_read_access)

                    async for row in conn.execute(query):
                        d = FileMetaData(**dict(row))
                        dex = FileMetaDataEx(
                            fmd=d, parent_id=str(Path(d.object_name).parent)
                        )
                        data.append(dex)

            if self.has_project_db:
                uuid_name_dict = {}
                # now parse the project to search for node/project names
                try:
                    async with self.engine.acquire() as conn:
                        async with conn.begin():
                            query = sa.select([projects]).where(
                                projects.c.uuid.in_(accesible_projects_ids)
                            )

                            async for row in conn.execute(query):
                                proj_data = dict(row.items())

                                uuid_name_dict[proj_data["uuid"]] = proj_data["name"]
                                wb = proj_data["workbench"]
                                for node in wb.keys():
                                    uuid_name_dict[node] = wb[node]["label"]
                except DBAPIError as _err:
                    logger.exception("Error querying database for project names")

                if not uuid_name_dict:
                    # there seems to be no project whatsoever for user_id
                    return []

                # only keep files from non-deleted project
                clean_data = deque()
                for dx in data:
                    d = dx.fmd
                    if d.project_id not in uuid_name_dict:
                        continue
                    #
                    # FIXME: artifically fills ['project_name', 'node_name', 'file_id', 'raw_file_path', 'display_file_path']
                    #        with information from the projects table!

                    d.project_name = uuid_name_dict[d.project_id]
                    if d.node_id in uuid_name_dict:
                        d.node_name = uuid_name_dict[d.node_id]

                    d.raw_file_path = str(
                        Path(d.project_id) / Path(d.node_id) / Path(d.file_name)
                    )
                    d.display_file_path = d.raw_file_path
                    d.file_id = d.file_uuid
                    if d.node_name and d.project_name:
                        d.display_file_path = str(
                            Path(d.project_name) / Path(d.node_name) / Path(d.file_name)
                        )
                        # once the data was sync to postgres metadata table at this point
                        clean_data.append(dx)

                data = clean_data

        elif location == DATCORE_STR:
            api_token, api_secret = self._get_datcore_tokens(user_id)
            dcw = DatcoreWrapper(api_token, api_secret, self.loop, self.pool)
            data = await dcw.list_files_raw()

        if uuid_filter:
            # TODO: incorporate this in db query!
            _query = re.compile(uuid_filter, re.IGNORECASE)
            filtered_data = deque()
            for dx in data:
                d = dx.fmd
                if _query.search(d.file_uuid):
                    filtered_data.append(dx)

            return list(filtered_data)

        if regex:
            _query = re.compile(regex, re.IGNORECASE)
            filtered_data = deque()
            for dx in data:
                d = dx.fmd
                _vars = vars(d)
                for v in _vars.keys():
                    if _query.search(v) or _query.search(str(_vars[v])):
                        filtered_data.append(dx)
                        break
            return list(filtered_data)

        return list(data)

    async def list_files_dataset(
        self, user_id: str, location: str, dataset_id: str
    ) -> Union[List[FileMetaData], List[FileMetaDataEx]]:
        # this is a cheap shot, needs fixing once storage/db is in sync
        data = []
        if location == SIMCORE_S3_STR:
            data: List[FileMetaDataEx] = await self.list_files(
                user_id, location, uuid_filter=dataset_id + "/"
            )

        elif location == DATCORE_STR:
            api_token, api_secret = self._get_datcore_tokens(user_id)
            dcw = DatcoreWrapper(api_token, api_secret, self.loop, self.pool)
            data: List[FileMetaData] = await dcw.list_files_raw_dataset(dataset_id)

        return data

    async def list_datasets(self, user_id: str, location: str) -> List[DatasetMetaData]:
        """Returns a list of top level datasets

        Works for simcore.s3 and datcore

        """
        data = []

        if location == SIMCORE_S3_STR:
            if self.has_project_db:
                try:
                    async with self.engine.acquire() as conn:
                        async with conn.begin():
                            readable_projects_ids = await get_readable_project_ids(
                                conn, int(user_id)
                            )
                            has_read_access = projects.c.uuid.in_(readable_projects_ids)

                            # FIXME: this DOES NOT read from file-metadata table!!!
                            query = sa.select([projects.c.uuid, projects.c.name]).where(
                                has_read_access
                            )
                            async for row in conn.execute(query):
                                dmd = DatasetMetaData(
                                    dataset_id=row.uuid,
                                    display_name=row.name,
                                )
                                data.append(dmd)
                except DBAPIError as _err:
                    logger.exception("Error querying database for project names")

        elif location == DATCORE_STR:
            api_token, api_secret = self._get_datcore_tokens(user_id)
            dcw = DatcoreWrapper(api_token, api_secret, self.loop, self.pool)
            data = await dcw.list_datasets()

        return data

    async def list_file(
        self, user_id: str, location: str, file_uuid: str
    ) -> Optional[FileMetaDataEx]:

        if location == SIMCORE_S3_STR:

            async with self.engine.acquire() as conn:
                async with conn.begin():
                    can: Optional[AccessRights] = await get_file_access_rights(
                        conn, int(user_id), file_uuid
                    )
                    if can.read:
                        query = sa.select([file_meta_data]).where(
                            file_meta_data.c.file_uuid == file_uuid
                        )
                        async for row in conn.execute(query):
                            d = FileMetaData(**dict(row))
                            dx = FileMetaDataEx(fmd=d, parent_id="")
                            return dx
                    else:
                        logger.debug("User %s was not read file %s", user_id, file_uuid)

        elif location == DATCORE_STR:
            # FIXME: review return inconsistencies
            api_token, api_secret = self._get_datcore_tokens(user_id)
            _dcw = DatcoreWrapper(api_token, api_secret, self.loop, self.pool)
            data = []  # await _dcw.list_file(file_uuid)
            return data

    # UPLOAD/DOWNLOAD LINKS ---------------------------

    async def upload_file_to_datcore(
        self, user_id: str, local_file_path: str, destination_id: str
    ):
        # uploads a locally available file to dat core given the storage path, optionally attached some meta data
        api_token, api_secret = self._get_datcore_tokens(user_id)
        dcw = DatcoreWrapper(api_token, api_secret, self.loop, self.pool)
        await dcw.upload_file_to_id(destination_id, local_file_path)

    async def _metadata_file_updater(
        self,
        file_uuid: str,
        bucket_name: str,
        object_name: str,
        file_size: int,
        last_modified: str,
        max_update_retries: int = 50,
    ):
        """
        Will retry max_update_retries to update the metadata on the file after an upload.
        If it is not successfull it will exit and log an error.

        Note: MinIO bucket notifications are not available with S3, that's why we have the
        following hacky solution
        """
        current_iteraction = 0

        session = aiobotocore.get_session()
        async with session.create_client(
            "s3",
            endpoint_url=self.s3_client.endpoint_url,
            aws_access_key_id=self.s3_client.access_key,
            aws_secret_access_key=self.s3_client.secret_key,
        ) as client:
            current_iteraction += 1
            continue_loop = True
            sleep_generator = expo()
            update_succeeded = False

            while continue_loop:
                result = await client.list_objects_v2(
                    Bucket=bucket_name, Prefix=object_name
                )
                sleep_amount = next(sleep_generator)
                continue_loop = current_iteraction <= max_update_retries

                if "Contents" not in result:
                    logger.info("File '%s' was not found in the bucket", object_name)
                    await asyncio.sleep(sleep_amount)
                    continue

                new_file_size = result["Contents"][0]["Size"]
                new_last_modified = str(result["Contents"][0]["LastModified"])
                if file_size == new_file_size or last_modified == new_last_modified:
                    logger.info("File '%s' did not change yet", object_name)
                    await asyncio.sleep(sleep_amount)
                    continue

                file_e_tag = result["Contents"][0]["ETag"].strip('"')
                # finally update the data in the database and exit
                continue_loop = False

                logger.info(
                    "Obtained this from S3: new_file_size=%s new_last_modified=%s file ETag=%s",
                    new_file_size,
                    new_last_modified,
                    file_e_tag,
                )

                async with self.engine.acquire() as conn:
                    query = (
                        file_meta_data.update()
                        .where(file_meta_data.c.file_uuid == file_uuid)
                        .values(
                            file_size=new_file_size,
                            last_modified=new_last_modified,
                            entity_tag=file_e_tag,
                        )
                    )  # primary key search is faster
                    await conn.execute(query)
                    update_succeeded = True
            if not update_succeeded:
                logger.error("Could not update file metadata for '%s'", file_uuid)

    async def upload_link(self, user_id: str, file_uuid: str):
        """
        Creates pre-signed upload link and updates metadata table when
        link is used and upload is successfuly completed

        SEE _metadata_file_updater
        """

        async with self.engine.acquire() as conn:
            can: Optional[AccessRights] = await get_file_access_rights(
                conn, int(user_id), file_uuid
            )
            if not can.write:
                logger.debug(
                    "User %s was not allowed to upload file %s", user_id, file_uuid
                )
                raise web.HTTPForbidden(
                    reason=f"User does not have enough access rights to upload file {file_uuid}"
                )

        @retry(**postgres_service_retry_policy_kwargs)
        async def _init_metadata() -> Tuple[int, str]:
            async with self.engine.acquire() as conn:
                fmd = FileMetaData()
                fmd.simcore_from_uuid(file_uuid, self.simcore_bucket_name)
                fmd.user_id = user_id  # NOTE: takes ownership of uploaded data

                query = sa.select([file_meta_data]).where(
                    file_meta_data.c.file_uuid == file_uuid
                )
                # if file already exists, we might want to update a time-stamp
                exists = await (await conn.execute(query)).scalar()
                if exists is None:
                    ins = file_meta_data.insert().values(**vars(fmd))
                    await conn.execute(ins)
                return fmd.file_size, fmd.last_modified

        file_size, last_modified = await _init_metadata()

        bucket_name = self.simcore_bucket_name
        object_name = file_uuid

        # a parallel task is tarted which will update the metadata of the updated file
        # once the update has finished.
        fire_and_forget_task(
            self._metadata_file_updater(
                file_uuid=file_uuid,
                bucket_name=bucket_name,
                object_name=object_name,
                file_size=file_size,
                last_modified=last_modified,
            )
        )
        return self.s3_client.create_presigned_put_url(bucket_name, object_name)

    async def download_link_s3(self, file_uuid: str, user_id: int) -> str:

        # access layer
        async with self.engine.acquire() as conn:
            can: Optional[AccessRights] = await get_file_access_rights(
                conn, int(user_id), file_uuid
            )
            if not can.read:
                # NOTE: this is tricky. A user with read access can download and data!
                # If write permission would be required, then shared projects as views cannot
                # recover data in nodes (e.g. jupyter cannot pull work data)
                #
                logger.debug(
                    "User %s was not allowed to download file %s", user_id, file_uuid
                )
                raise web.HTTPForbidden(
                    reason=f"User does not have enough rights to download {file_uuid}"
                )

        link = None
        bucket_name = self.simcore_bucket_name
        object_name = file_uuid
        link = self.s3_client.create_presigned_get_url(bucket_name, object_name)
        return link

    async def download_link_datcore(self, user_id: str, file_id: str) -> Dict[str, str]:
        link = ""
        api_token, api_secret = self._get_datcore_tokens(user_id)
        dcw = DatcoreWrapper(api_token, api_secret, self.loop, self.pool)
        link, filename = await dcw.download_link_by_id(file_id)
        return link, filename

    # COPY -----------------------------

    async def copy_file_s3_s3(self, user_id: str, dest_uuid: str, source_uuid: str):
        # FIXME: operation MUST be atomic

        # source is s3, location is s3
        to_bucket_name = self.simcore_bucket_name
        to_object_name = dest_uuid
        from_bucket = self.simcore_bucket_name
        from_object_name = source_uuid
        from_bucket_object_name = os.path.join(from_bucket, from_object_name)
        # FIXME: This is not async!
        self.s3_client.copy_object(
            to_bucket_name, to_object_name, from_bucket_object_name
        )

        # update db
        async with self.engine.acquire() as conn:
            fmd = FileMetaData()
            fmd.simcore_from_uuid(dest_uuid, self.simcore_bucket_name)
            fmd.user_id = user_id
            ins = file_meta_data.insert().values(**vars(fmd))
            await conn.execute(ins)

    async def copy_file_s3_datcore(
        self, user_id: str, dest_uuid: str, source_uuid: str
    ):
        session = get_client_session(self.app)

        # source is s3, get link and copy to datcore
        bucket_name = self.simcore_bucket_name
        object_name = source_uuid
        filename = source_uuid.split("/")[-1]

        s3_dowload_link = self.s3_client.create_presigned_get_url(
            bucket_name, object_name
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            # FIXME: connect download and upload streams
            local_file_path = os.path.join(tmpdir, filename)

            # Downloads S3 -> local
            await download_to_file_or_raise(session, s3_dowload_link, local_file_path)

            # Uploads local -> DATCore
            await self.upload_file_to_datcore(
                user_id=user_id,
                local_file_path=local_file_path,
                destination_id=dest_uuid,
            )

    async def copy_file_datcore_s3(
        self,
        user_id: str,
        dest_uuid: str,
        source_uuid: str,
        filename_missing: bool = False,
    ):
        session = get_client_session(self.app)

        # 2 steps: Get download link for local copy, the upload link to s3
        # TODO: This should be a redirect stream!
        dc_link, filename = await self.download_link_datcore(
            user_id=user_id, file_id=source_uuid
        )
        if filename_missing:
            dest_uuid = str(Path(dest_uuid) / filename)

        s3_upload_link = await self.upload_link(user_id, dest_uuid)

        with tempfile.TemporaryDirectory() as tmpdir:
            # FIXME: connect download and upload streams

            local_file_path = os.path.join(tmpdir, filename)

            # Downloads DATCore -> local
            await download_to_file_or_raise(session, dc_link, local_file_path)

            # Uploads local -> S3
            s3_upload_link = URL(s3_upload_link)
            async with session.put(
                s3_upload_link,
                data=Path(local_file_path).open("rb"),
                raise_for_status=True,
            ) as resp:
                logger.debug(
                    "Uploaded local -> SIMCore %s . Status %s",
                    s3_upload_link,
                    resp.status,
                )

        return dest_uuid

    async def copy_file(
        self,
        user_id: str,
        dest_location: str,
        dest_uuid: str,
        source_location: str,
        source_uuid: str,
    ):
        if source_location == SIMCORE_S3_STR:
            if dest_location == DATCORE_STR:
                await self.copy_file_s3_datcore(user_id, dest_uuid, source_uuid)
            elif dest_location == SIMCORE_S3_STR:
                await self.copy_file_s3_s3(user_id, dest_uuid, source_uuid)
        elif source_location == DATCORE_STR:
            if dest_location == DATCORE_STR:
                raise NotImplementedError("copy files from datcore 2 datcore not impl")
            if dest_location == SIMCORE_S3_STR:
                await self.copy_file_datcore_s3(user_id, dest_uuid, source_uuid)

    async def deep_copy_project_simcore_s3(
        self, user_id: str, source_project, destination_project, node_mapping
    ):
        """Parses a given source project and copies all related files to the destination project

        Since all files are organized as

            project_id/node_id/filename or links to datcore

        this function creates a new folder structure

            project_id/node_id/filename

        and copies all files to the corresponding places.

        Additionally, all external files from datcore are being copied and the paths in the destination
        project are adapted accordingly

        Lastly, the meta data db is kept in sync
        """
        source_folder = source_project["uuid"]
        dest_folder = destination_project["uuid"]

        # access layer
        async with self.engine.acquire() as conn:
            async with conn.begin():
                can = await get_project_access_rights(
                    conn, int(user_id), project_id=source_folder
                )
                if not can.read:
                    logger.debug(
                        "User %s was not allowed to copy project %s",
                        user_id,
                        source_folder,
                    )
                    raise web.HTTPForbidden(
                        reason=f"User does not have enough access rights to copy project '{source_folder}'"
                    )
                can = await get_project_access_rights(
                    conn, int(user_id), project_id=dest_folder
                )
                if not can.write:
                    logger.debug(
                        "User %s was not allowed to copy project %s",
                        user_id,
                        dest_folder,
                    )
                    raise web.HTTPForbidden(
                        reason=f"User does not have enough access rights to copy project '{dest_folder}'"
                    )

        # build up naming map based on labels
        uuid_name_dict = {}
        uuid_name_dict[dest_folder] = destination_project["name"]
        for src_node_id, src_node in source_project["workbench"].items():
            new_node_id = node_mapping.get(src_node_id)
            if new_node_id is not None:
                uuid_name_dict[new_node_id] = src_node["label"]

        # Step 1: List all objects for this project replace them with the destination object name and do a copy at the same time collect some names
        session = aiobotocore.get_session()
        async with session.create_client(
            "s3",
            endpoint_url=self.s3_client.endpoint_url,
            aws_access_key_id=self.s3_client.access_key,
            aws_secret_access_key=self.s3_client.secret_key,
        ) as client:
            response = await client.list_objects_v2(
                Bucket=self.simcore_bucket_name, Prefix=source_folder
            )

            if "Contents" in response:
                for f in response["Contents"]:
                    source_object_name = f["Key"]
                    source_object_parts = Path(source_object_name).parts

                    if len(source_object_parts) == 3:
                        old_node_id = source_object_parts[1]
                        new_node_id = node_mapping.get(old_node_id)
                        if new_node_id is not None:
                            old_filename = source_object_parts[2]
                            dest_object_name = str(
                                Path(dest_folder) / new_node_id / old_filename
                            )
                            copy_source = {
                                "Bucket": self.simcore_bucket_name,
                                "Key": source_object_name,
                            }
                            response = await client.copy_object(
                                CopySource=copy_source,
                                Bucket=self.simcore_bucket_name,
                                Key=dest_object_name,
                            )
                    else:
                        # This may happen once we have shared/home folders
                        logger.info("len(object.parts != 3")

            # Step 2: List all references in outputs that point to datcore and copy over
            for node_id, node in destination_project["workbench"].items():
                outputs: Dict = node.get("outputs", {})
                for _output_key, output in outputs.items():
                    if "store" in output and output["store"] == DATCORE_ID:
                        src = output["path"]
                        dest = str(Path(dest_folder) / node_id)
                        logger.info("Need to copy %s to %s", src, dest)
                        dest = await self.copy_file_datcore_s3(
                            user_id=user_id,
                            dest_uuid=dest,
                            source_uuid=src,
                            filename_missing=True,
                        )
                        # and change the dest project accordingly
                        output["store"] = SIMCORE_S3_ID
                        output["path"] = dest
                    elif "store" in output and output["store"] == SIMCORE_S3_ID:
                        source = output["path"]
                        dest = dest = str(
                            Path(dest_folder) / node_id / Path(source).name
                        )
                        output["store"] = SIMCORE_S3_ID
                        output["path"] = dest

        # step 3: list files first to create fmds
        session = aiobotocore.get_session()
        fmds = []
        async with session.create_client(
            "s3",
            endpoint_url=self.s3_client.endpoint_url,
            aws_access_key_id=self.s3_client.access_key,
            aws_secret_access_key=self.s3_client.secret_key,
        ) as client:
            response = await client.list_objects_v2(
                Bucket=self.simcore_bucket_name, Prefix=dest_folder + "/"
            )
            if "Contents" in response:
                for f in response["Contents"]:
                    fmd = FileMetaData()
                    fmd.simcore_from_uuid(f["Key"], self.simcore_bucket_name)
                    fmd.project_name = uuid_name_dict.get(dest_folder, "Untitled")
                    fmd.node_name = uuid_name_dict.get(fmd.node_id, "Untitled")
                    fmd.raw_file_path = fmd.file_uuid
                    fmd.display_file_path = str(
                        Path(fmd.project_name) / fmd.node_name / fmd.file_name
                    )
                    fmd.user_id = user_id
                    fmd.file_size = f["Size"]
                    fmd.last_modified = str(f["LastModified"])
                    fmds.append(fmd)

        # step 4 sync db
        async with self.engine.acquire() as conn:
            async with conn.begin():
                # TODO: upsert in one statment of ALL
                for fmd in fmds:
                    query = sa.select([file_meta_data]).where(
                        file_meta_data.c.file_uuid == fmd.file_uuid
                    )
                    # if file already exists, we might w
                    rows = await conn.execute(query)
                    exists = await rows.scalar()
                    if exists:
                        delete_me = file_meta_data.delete().where(
                            file_meta_data.c.file_uuid == fmd.file_uuid
                        )
                        await conn.execute(delete_me)
                    ins = file_meta_data.insert().values(**vars(fmd))
                    await conn.execute(ins)

    # DELETE -------------------------------------

    async def delete_file(self, user_id: str, location: str, file_uuid: str):
        """Deletes a file given its fmd and location

        Additionally requires a user_id for 3rd party auth

        For internal storage, the db state should be updated upon completion via
        Notification mechanism

        For simcore.s3 we can use the file_name
        For datcore we need the full path
        """
        if location == SIMCORE_S3_STR:
            # FIXME: operation MUST be atomic, transaction??

            to_delete = []
            async with self.engine.acquire() as conn:
                async with conn.begin():
                    can: Optional[AccessRights] = await get_file_access_rights(
                        conn, int(user_id), file_uuid
                    )
                    if not can.delete:
                        logger.debug(
                            "User %s was not allowed to delete file %s",
                            user_id,
                            file_uuid,
                        )
                        raise web.HTTPForbidden(
                            reason=f"User '{user_id}' does not have enough access rights to delete file {file_uuid}"
                        )

                    query = sa.select(
                        [file_meta_data.c.bucket_name, file_meta_data.c.object_name]
                    ).where(file_meta_data.c.file_uuid == file_uuid)

                    async for row in conn.execute(query):
                        if self.s3_client.remove_objects(
                            row.bucket_name, [row.object_name]
                        ):
                            to_delete.append(file_uuid)

                    await conn.execute(
                        file_meta_data.delete().where(
                            file_meta_data.c.file_uuid.in_(to_delete)
                        )
                    )

        elif location == DATCORE_STR:
            # FIXME: review return inconsistencies
            api_token, api_secret = self._get_datcore_tokens(user_id)
            dcw = DatcoreWrapper(api_token, api_secret, self.loop, self.pool)
            # destination, filename = _parse_datcore(file_uuid)
            file_id = file_uuid
            return await dcw.delete_file_by_id(file_id)

    async def delete_project_simcore_s3(
        self, user_id: str, project_id: str, node_id: Optional[str] = None
    ) -> web.Response:

        """Deletes all files from a given node in a project in simcore.s3 and updated db accordingly.
        If node_id is not given, then all the project files db entries are deleted.
        """

        # FIXME: operation MUST be atomic. Mark for deletion and remove from db when deletion fully confirmed

        async with self.engine.acquire() as conn:
            async with conn.begin():
                # access layer
                can: Optional[AccessRights] = await get_project_access_rights(
                    conn, int(user_id), project_id
                )
                if not can.delete:
                    logger.debug(
                        "User %s was not allowed to delete project %s",
                        user_id,
                        project_id,
                    )
                    raise web.HTTPForbidden(
                        reason=f"User does not have delete access for {project_id}"
                    )

                delete_me = file_meta_data.delete().where(
                    file_meta_data.c.project_id == project_id,
                )
                if node_id:
                    delete_me = delete_me.where(file_meta_data.c.node_id == node_id)
                await conn.execute(delete_me)

        session = aiobotocore.get_session()
        async with session.create_client(
            "s3",
            endpoint_url=self.s3_client.endpoint_url,
            aws_access_key_id=self.s3_client.access_key,
            aws_secret_access_key=self.s3_client.secret_key,
        ) as client:
            response = await client.list_objects_v2(
                Bucket=self.simcore_bucket_name,
                Prefix=f"{project_id}/{node_id}/" if node_id else f"{project_id}/",
            )
            if "Contents" in response:
                objects_to_delete = []
                for f in response["Contents"]:
                    objects_to_delete.append({"Key": f["Key"]})

                if objects_to_delete:
                    response = await client.delete_objects(
                        Bucket=self.simcore_bucket_name,
                        Delete={"Objects": objects_to_delete},
                    )
                    return response

    # SEARCH -------------------------------------

    async def search_files_starting_with(
        self, user_id: int, prefix: str
    ) -> List[FileMetaDataEx]:
        # Avoids using list_files since it accounts for projects/nodes
        # Storage should know NOTHING about those concepts
        files_meta = deque()

        async with self.engine.acquire() as conn:
            async with conn.begin():
                # access layer
                can_read_projects_ids = await get_readable_project_ids(
                    conn, int(user_id)
                )
                has_read_access = (
                    file_meta_data.c.user_id == str(user_id)
                ) | file_meta_data.c.project_id.in_(can_read_projects_ids)

                stmt = sa.select([file_meta_data]).where(
                    file_meta_data.c.file_uuid.startswith(prefix) & has_read_access
                )

                async for row in conn.execute(stmt):
                    meta = FileMetaData(**dict(row))
                    meta_extended = FileMetaDataEx(
                        fmd=meta,
                        parent_id=str(Path(meta.object_name).parent),
                    )
                    files_meta.append(meta_extended)
        return list(files_meta)
