import asyncio
import contextlib
import functools
import logging
import urllib.parse
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Protocol, cast

import aioboto3
from aiobotocore.session import ClientCreatorContext
from boto3.s3.transfer import TransferConfig
from botocore import exceptions as botocore_exc
from botocore.client import Config
from models_library.api_schemas_storage.storage_schemas import (
    ETag,
    S3BucketName,
    UploadedPart,
)
from models_library.basic_types import SHA256Str
from models_library.bytes_iters import BytesIter, DataSize
from pydantic import AnyUrl, ByteSize, TypeAdapter
from servicelib.bytes_iters import DEFAULT_READ_CHUNK_SIZE, BytesStreamer
from servicelib.logging_utils import log_catch, log_context
from servicelib.s3_utils import FileLikeReader
from servicelib.utils import limited_gather
from settings_library.s3 import S3Settings
from types_aiobotocore_s3 import S3Client
from types_aiobotocore_s3.literals import BucketLocationConstraintType
from types_aiobotocore_s3.type_defs import (
    ListObjectsV2RequestTypeDef,
    ObjectIdentifierTypeDef,
)

from ._constants import (
    MULTIPART_COPY_THRESHOLD,
    MULTIPART_UPLOADS_MIN_TOTAL_SIZE,
    S3_OBJECT_DELIMITER,
)
from ._error_handler import s3_exception_handler, s3_exception_handler_async_gen
from ._errors import S3DestinationNotEmptyError, S3KeyNotFoundError
from ._models import (
    MultiPartUploadLinks,
    PathCursor,
    S3DirectoryMetaData,
    S3MetaData,
    S3ObjectKey,
    S3ObjectPrefix,
    UploadID,
)
from ._utils import compute_num_file_chunks, create_final_prefix

_logger = logging.getLogger(__name__)

_S3_MAX_CONCURRENCY_DEFAULT: Final[int] = 10
_DEFAULT_AWS_REGION: Final[str] = "us-east-1"
_MAX_ITEMS_PER_PAGE: Final[int] = 500
_MAX_CONCURRENT_COPY: Final[int] = 4
_AWS_MAX_ITEMS_PER_PAGE: Final[int] = 1000


ListAnyUrlTypeAdapter: Final[TypeAdapter[list[AnyUrl]]] = TypeAdapter(list[AnyUrl])


class UploadedBytesTransferredCallback(Protocol):
    def __call__(self, bytes_transferred: int, *, file_name: str) -> None: ...


class CopiedBytesTransferredCallback(Protocol):
    def __call__(self, total_bytes_copied: int, *, file_name: str) -> None: ...


@dataclass(frozen=True)
class SimcoreS3API:  # pylint: disable=too-many-public-methods
    _client: S3Client
    _session: aioboto3.Session
    _exit_stack: contextlib.AsyncExitStack = field(
        default_factory=contextlib.AsyncExitStack
    )
    transfer_max_concurrency: int = _S3_MAX_CONCURRENCY_DEFAULT

    @classmethod
    async def create(
        cls, settings: S3Settings, s3_max_concurrency: int = _S3_MAX_CONCURRENCY_DEFAULT
    ) -> "SimcoreS3API":
        session = aioboto3.Session()
        session_client = None
        exit_stack = contextlib.AsyncExitStack()
        try:
            config = Config(
                # This setting tells the S3 client to only calculate checksums when explicitly required
                # by the operation. This avoids unnecessary checksum calculations for operations that
                # don't need them, improving performance.
                # See: https://boto3.amazonaws.com/v1/documentation/api/latest/guide/s3.html#calculating-checksums
                signature_version="s3v4",
                request_checksum_calculation="when_required",  # type: ignore[call-arg]
            )
            session_client = session.client(  # type: ignore[call-overload]
                "s3",
                endpoint_url=f"{settings.S3_ENDPOINT}",
                aws_access_key_id=settings.S3_ACCESS_KEY,
                aws_secret_access_key=settings.S3_SECRET_KEY,
                region_name=settings.S3_REGION,
                config=config,
            )
            assert isinstance(session_client, ClientCreatorContext)  # nosec

            s3_client = cast(
                S3Client, await exit_stack.enter_async_context(session_client)
            )
            # NOTE: this triggers a botocore.exception.ClientError in case the connection is not made to the S3 backend
            await s3_client.list_buckets()

            return cls(s3_client, session, exit_stack, s3_max_concurrency)
        except Exception:
            await exit_stack.aclose()

            raise

    async def close(self) -> None:
        await self._exit_stack.aclose()

    async def http_check_bucket_connected(self, *, bucket: S3BucketName) -> bool:
        with log_catch(_logger, reraise=False):
            return await self.bucket_exists(bucket=bucket)
        return False

    @s3_exception_handler(_logger)
    async def create_bucket(
        self,
        *,
        bucket: S3BucketName,
        region: BucketLocationConstraintType | Literal["us-east-1"],
    ) -> None:
        with log_context(
            _logger, logging.INFO, msg=f"Create bucket {bucket} in {region}"
        ):
            try:
                # NOTE: see https://github.com/boto/boto3/issues/125 why this is so... (sic)
                # setting it for the us-east-1 creates issue when creating buckets
                create_bucket_config: dict[str, Any] = {"Bucket": f"{bucket}"}
                if region != _DEFAULT_AWS_REGION:
                    create_bucket_config["CreateBucketConfiguration"] = {
                        "LocationConstraint": region
                    }

                await self._client.create_bucket(**create_bucket_config)

            except self._client.exceptions.BucketAlreadyOwnedByYou:
                _logger.info(
                    "Bucket %s already exists and is owned by us",
                    bucket,
                )

    @s3_exception_handler(_logger)
    async def bucket_exists(self, *, bucket: S3BucketName) -> bool:
        """
        :raises: S3AccessError for any other error
        """
        try:
            await self._client.head_bucket(Bucket=bucket)
            return True
        except botocore_exc.ClientError as exc:
            status_code = exc.response.get("Error", {}).get("Code", -1)
            if status_code == "404":
                return False
            raise

    @s3_exception_handler(_logger)
    async def object_exists(
        self, *, bucket: S3BucketName, object_key: S3ObjectKey
    ) -> bool:
        # SEE https://www.peterbe.com/plog/fastest-way-to-find-out-if-a-file-exists-in-s3
        response = await self._client.list_objects_v2(Bucket=bucket, Prefix=object_key)
        return len(response.get("Contents", [])) > 0

    @s3_exception_handler(_logger)
    async def get_object_metadata(
        self, *, bucket: S3BucketName, object_key: S3ObjectKey
    ) -> S3MetaData:
        response = await self._client.head_object(
            Bucket=bucket, Key=object_key, ChecksumMode="ENABLED"
        )
        return S3MetaData.from_botocore_head_object(object_key, response)

    @s3_exception_handler(_logger)
    async def get_directory_metadata(
        self, *, bucket: S3BucketName, prefix: str
    ) -> S3DirectoryMetaData:
        size = 0
        async for s3_object in self._list_all_objects(bucket=bucket, prefix=prefix):
            size += s3_object.size
        return S3DirectoryMetaData(prefix=S3ObjectPrefix(prefix), size=ByteSize(size))

    @s3_exception_handler(_logger)
    async def count_objects(
        self,
        *,
        bucket: S3BucketName,
        prefix: S3ObjectPrefix | None,
        start_after: S3ObjectKey | None,
        is_partial_prefix: bool = False,
        use_delimiter: bool = True,
    ) -> int:
        """returns the number of entries in the bucket, defined
        by prefix and start_after same as list_objects
        """
        paginator = self._client.get_paginator("list_objects_v2")
        total_count = 0
        async for page in paginator.paginate(
            Bucket=bucket,
            Prefix=create_final_prefix(prefix, is_partial_prefix=is_partial_prefix),
            StartAfter=start_after or "",
            Delimiter=S3_OBJECT_DELIMITER if use_delimiter else "",
        ):
            total_count += page.get("KeyCount", 0)
        return total_count

    @s3_exception_handler(_logger)
    async def list_objects(
        self,
        *,
        bucket: S3BucketName,
        prefix: S3ObjectPrefix | None,
        start_after: S3ObjectKey | None,
        limit: int = _MAX_ITEMS_PER_PAGE,
        next_cursor: PathCursor | None = None,
        is_partial_prefix: bool = False,
    ) -> tuple[list[S3MetaData | S3DirectoryMetaData], PathCursor | None]:
        """returns a number of entries in the bucket, defined by limit
        the entries are sorted alphabetically by key. If a cursor is returned
        then the client can call the function again with the cursor to get the
        next entries.

        the first entry is defined by start_after
        if start_after is None, the first entry is the first one in the bucket
        if prefix is not None, only entries with the given prefix are returned
        if prefix is None, all entries in the bucket are returned
        if next_cursor is set, then the call will return the next entries after the cursor
        if is_partial_prefix is set then the prefix is not auto-delimited
        (if False equivalent to `ls /home/user/`
        if True equivalent to `ls /home/user*`)
        limit must be >= 1 and <= _AWS_MAX_ITEMS_PER_PAGE

        Raises:
            ValueError: in case of invalid limit
        """
        if limit < 1:
            msg = "num_objects must be >= 1"
            raise ValueError(msg)
        if limit > _AWS_MAX_ITEMS_PER_PAGE:
            msg = f"num_objects must be <= {_AWS_MAX_ITEMS_PER_PAGE}"
            raise ValueError(msg)

        list_config: ListObjectsV2RequestTypeDef = {
            "Bucket": bucket,
            "Prefix": create_final_prefix(prefix, is_partial_prefix=is_partial_prefix),
            "MaxKeys": limit,
            "Delimiter": S3_OBJECT_DELIMITER,
        }
        if start_after:
            list_config["StartAfter"] = start_after
        if next_cursor:
            list_config["ContinuationToken"] = next_cursor
        listed_objects = await self._client.list_objects_v2(**list_config)
        found_objects: list[S3MetaData | S3DirectoryMetaData] = []
        if "CommonPrefixes" in listed_objects:
            # we have folders here
            list_subfolders = listed_objects["CommonPrefixes"]
            found_objects.extend(
                S3DirectoryMetaData.model_construct(
                    prefix=S3ObjectPrefix(subfolder["Prefix"], size=None)
                )
                for subfolder in list_subfolders
                if "Prefix" in subfolder
            )
        if "Contents" in listed_objects:
            found_objects.extend(
                S3MetaData.from_botocore_list_objects(obj)
                for obj in listed_objects["Contents"]
            )
        next_cursor = None
        if listed_objects["IsTruncated"]:
            next_cursor = listed_objects["NextContinuationToken"]
        return found_objects, next_cursor

    @s3_exception_handler_async_gen(_logger)
    async def list_objects_paginated(
        self,
        bucket: S3BucketName,
        prefix: str,
        *,
        items_per_page: int = _MAX_ITEMS_PER_PAGE,
    ) -> AsyncGenerator[list[S3MetaData], None]:
        if items_per_page > _AWS_MAX_ITEMS_PER_PAGE:
            msg = f"items_per_page must be <= {_AWS_MAX_ITEMS_PER_PAGE}"
            raise ValueError(msg)
        async for page in self._client.get_paginator("list_objects_v2").paginate(
            Bucket=bucket,
            Prefix=prefix,
            PaginationConfig={
                "PageSize": items_per_page,
            },
        ):
            yield [
                S3MetaData.from_botocore_list_objects(obj)
                for obj in page.get("Contents", [])
            ]

    async def _list_all_objects(
        self, *, bucket: S3BucketName, prefix: str
    ) -> AsyncGenerator[S3MetaData, None]:
        async for s3_objects in self.list_objects_paginated(
            bucket=bucket, prefix=prefix
        ):
            for obj in s3_objects:
                yield obj

    @s3_exception_handler(_logger)
    async def delete_objects_recursively(
        self, *, bucket: S3BucketName, prefix: str
    ) -> None:
        # NOTE: deletion of objects is done in batches of max 1000 elements,
        # the maximum accepted by the S3 API
        with log_context(
            _logger, logging.DEBUG, f"deleting objects in {prefix=}", log_duration=True
        ):
            async for s3_objects in self.list_objects_paginated(
                bucket=bucket, prefix=prefix
            ):
                objects_to_delete: Sequence[ObjectIdentifierTypeDef] = [
                    {"Key": f"{_.object_key}"} for _ in s3_objects
                ]
                if objects_to_delete:
                    await self._client.delete_objects(
                        Bucket=bucket,
                        Delete={"Objects": objects_to_delete},
                    )

    @s3_exception_handler(_logger)
    async def delete_object(
        self, *, bucket: S3BucketName, object_key: S3ObjectKey
    ) -> None:
        await self._client.delete_object(Bucket=bucket, Key=object_key)

    @s3_exception_handler(_logger)
    async def undelete_object(
        self, *, bucket: S3BucketName, object_key: S3ObjectKey
    ) -> None:
        """this allows to restore a file that was deleted.
        **NOT to restore previous versions!"""
        with log_context(
            _logger, logging.DEBUG, msg=f"undeleting {bucket}/{object_key}"
        ):
            response = await self._client.list_object_versions(
                Bucket=bucket, Prefix=object_key, MaxKeys=1
            )
            _logger.debug("%s", f"{response=}")
            if not response["IsTruncated"] and all(
                _ not in response for _ in ("Versions", "DeleteMarkers")
            ):
                raise S3KeyNotFoundError(key=object_key, bucket=bucket)
            if "DeleteMarkers" in response:
                # we have something to undelete
                latest_version = response["DeleteMarkers"][0]
                assert "IsLatest" in latest_version  # nosec
                assert "VersionId" in latest_version  # nosec
                await self._client.delete_object(
                    Bucket=bucket,
                    Key=object_key,
                    VersionId=latest_version["VersionId"],
                )
                _logger.debug("restored %s", f"{bucket}/{object_key}")

    @s3_exception_handler(_logger)
    async def create_single_presigned_download_link(
        self,
        *,
        bucket: S3BucketName,
        object_key: S3ObjectKey,
        expiration_secs: int,
    ) -> AnyUrl:
        # NOTE: ensure the bucket/object exists, this will raise if not
        await self._client.head_bucket(Bucket=bucket)
        await self._client.head_object(Bucket=bucket, Key=object_key)
        generated_link = await self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": object_key},
            ExpiresIn=expiration_secs,
        )
        return TypeAdapter(AnyUrl).validate_python(generated_link)

    @s3_exception_handler(_logger)
    async def create_single_presigned_upload_link(
        self, *, bucket: S3BucketName, object_key: S3ObjectKey, expiration_secs: int
    ) -> AnyUrl:
        # NOTE: ensure the bucket/object exists, this will raise if not
        await self._client.head_bucket(Bucket=bucket)
        generated_link = await self._client.generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": object_key},
            ExpiresIn=expiration_secs,
        )
        return TypeAdapter(AnyUrl).validate_python(generated_link)

    @s3_exception_handler(_logger)
    async def create_multipart_upload_links(
        self,
        *,
        bucket: S3BucketName,
        object_key: S3ObjectKey,
        file_size: ByteSize,
        expiration_secs: int,
        sha256_checksum: SHA256Str | None,
    ) -> MultiPartUploadLinks:
        # NOTE: ensure the bucket exists, this will raise if not
        await self._client.head_bucket(Bucket=bucket)
        # first initiate the multipart upload
        create_input: dict[str, Any] = {"Bucket": bucket, "Key": object_key}
        if sha256_checksum:
            create_input["Metadata"] = {"sha256_checksum": sha256_checksum}
        response = await self._client.create_multipart_upload(**create_input)
        upload_id = response["UploadId"]
        # compute the number of links, based on the announced file size
        num_upload_links, chunk_size = compute_num_file_chunks(file_size)
        # now create the links
        upload_links = ListAnyUrlTypeAdapter.validate_python(
            await asyncio.gather(
                *(
                    self._client.generate_presigned_url(
                        "upload_part",
                        Params={
                            "Bucket": bucket,
                            "Key": object_key,
                            "PartNumber": i + 1,
                            "UploadId": upload_id,
                        },
                        ExpiresIn=expiration_secs,
                    )
                    for i in range(num_upload_links)
                ),
            ),
        )
        return MultiPartUploadLinks(
            upload_id=upload_id, chunk_size=chunk_size, urls=upload_links
        )

    @s3_exception_handler(_logger)
    async def list_ongoing_multipart_uploads(
        self,
        *,
        bucket: S3BucketName,
    ) -> list[tuple[UploadID, S3ObjectKey]]:
        """Returns all the currently ongoing multipart uploads

        NOTE: minio does not implement the same behaviour as AWS here and will
        only return the uploads if a prefix or object name is given [minio issue](https://github.com/minio/minio/issues/7632).

        :return: list of AWS uploads see [boto3 documentation](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html#S3.Client.list_multipart_uploads)
        """
        response = await self._client.list_multipart_uploads(
            Bucket=bucket,
        )

        return [
            (
                upload.get("UploadId", "undefined-uploadid"),
                S3ObjectKey(upload.get("Key", "undefined-key")),
            )
            for upload in response.get("Uploads", [])
        ]

    @s3_exception_handler(_logger)
    async def abort_multipart_upload(
        self, *, bucket: S3BucketName, object_key: S3ObjectKey, upload_id: UploadID
    ) -> None:
        await self._client.abort_multipart_upload(
            Bucket=bucket, Key=object_key, UploadId=upload_id
        )

    @s3_exception_handler(_logger)
    async def complete_multipart_upload(
        self,
        *,
        bucket: S3BucketName,
        object_key: S3ObjectKey,
        upload_id: UploadID,
        uploaded_parts: list[UploadedPart],
    ) -> ETag:
        inputs: dict[str, Any] = {
            "Bucket": bucket,
            "Key": object_key,
            "UploadId": upload_id,
            "MultipartUpload": {
                "Parts": [
                    {"ETag": part.e_tag, "PartNumber": part.number}
                    for part in uploaded_parts
                ]
            },
        }
        response = await self._client.complete_multipart_upload(**inputs)
        return response["ETag"]

    @s3_exception_handler(_logger)
    async def upload_file(
        self,
        *,
        bucket: S3BucketName,
        file: Path,
        object_key: S3ObjectKey,
        bytes_transfered_cb: UploadedBytesTransferredCallback | None,
    ) -> None:
        """upload a file using aioboto3 transfer manager (e.g. works >5Gb and creates multiple threads)"""
        upload_options: dict[str, Any] = {
            "Bucket": bucket,
            "Key": object_key,
            "Config": TransferConfig(max_concurrency=self.transfer_max_concurrency),
        }
        if bytes_transfered_cb:
            upload_options |= {
                "Callback": functools.partial(
                    bytes_transfered_cb, file_name=f"{object_key}"
                )
            }
        await self._client.upload_file(f"{file}", **upload_options)

    @s3_exception_handler(_logger)
    async def copy_object(
        self,
        *,
        bucket: S3BucketName,
        src_object_key: S3ObjectKey,
        dst_object_key: S3ObjectKey,
        bytes_transfered_cb: CopiedBytesTransferredCallback | None,
        object_metadata: S3MetaData | None = None,
    ) -> None:
        """copy a file in S3 using aioboto3 transfer manager (e.g. works >5Gb and creates multiple threads)"""
        copy_options: dict[str, Any] = {
            "CopySource": {"Bucket": bucket, "Key": src_object_key},
            "Bucket": bucket,
            "Key": dst_object_key,
            "Config": TransferConfig(
                max_concurrency=self.transfer_max_concurrency,
                multipart_threshold=MULTIPART_COPY_THRESHOLD,
            ),
        }
        if bytes_transfered_cb:
            copy_options |= {
                "Callback": functools.partial(
                    bytes_transfered_cb, file_name=f"{dst_object_key}"
                )
            }
        # NOTE: boto3 copy function uses copy_object until 'multipart_threshold' is reached then switches to multipart copy
        # copy_object does not provide any callbacks so we can't track progress so we need to ensure at least the completion
        # of the object is tracked
        await self._client.copy(**copy_options)
        if bytes_transfered_cb:
            if object_metadata is None:
                object_metadata = await self.get_object_metadata(
                    bucket=bucket, object_key=dst_object_key
                )
            bytes_transfered_cb(object_metadata.size, file_name=f"{dst_object_key}")

    @s3_exception_handler(_logger)
    async def copy_objects_recursively(
        self,
        *,
        bucket: S3BucketName,
        src_prefix: str,
        dst_prefix: str,
        bytes_transfered_cb: CopiedBytesTransferredCallback | None,
    ) -> None:
        """copy from 1 location in S3 to another recreating the same structure"""
        dst_metadata = await self.get_directory_metadata(
            bucket=bucket, prefix=dst_prefix
        )
        if dst_metadata.size and dst_metadata.size > 0:
            raise S3DestinationNotEmptyError(dst_prefix=dst_prefix)
        await limited_gather(
            *[
                self.copy_object(
                    bucket=bucket,
                    src_object_key=s3_object.object_key,
                    dst_object_key=s3_object.object_key.replace(src_prefix, dst_prefix),
                    bytes_transfered_cb=bytes_transfered_cb,
                    object_metadata=s3_object,
                )
                async for s3_object in self._list_all_objects(
                    bucket=bucket, prefix=src_prefix
                )
            ],
            limit=_MAX_CONCURRENT_COPY,
        )

    async def get_bytes_streamer_from_object(
        self,
        bucket_name: S3BucketName,
        object_key: S3ObjectKey,
        *,
        chunk_size: int = DEFAULT_READ_CHUNK_SIZE,
    ) -> BytesStreamer:
        """stream read an object from S3 chunk by chunk"""

        # NOTE `download_fileobj` cannot be used to implement this because
        # it will buffer the entire file in memory instead of reading it
        # chunk by chunk

        # below is a quick call
        head_response = await self._client.head_object(
            Bucket=bucket_name, Key=object_key
        )
        data_size = DataSize(head_response["ContentLength"])

        async def _() -> BytesIter:
            # Download the file in chunks
            position = 0
            while position < data_size:
                # Calculate the range for this chunk
                end = min(position + chunk_size - 1, data_size - 1)
                range_header = f"bytes={position}-{end}"

                # Download the chunk
                response = await self._client.get_object(
                    Bucket=bucket_name, Key=object_key, Range=range_header
                )

                chunk = await response["Body"].read()

                # Yield the chunk for processing
                yield chunk

                position += chunk_size

        return BytesStreamer(data_size, _)

    @s3_exception_handler(_logger)
    async def upload_object_from_file_like(
        self,
        bucket_name: S3BucketName,
        object_key: S3ObjectKey,
        file_like_reader: FileLikeReader,
    ) -> None:
        """streams write an object in S3 from an AsyncIterable[bytes]"""
        await self._client.upload_fileobj(file_like_reader, bucket_name, object_key)  # type: ignore[arg-type]

    @staticmethod
    def is_multipart(file_size: ByteSize) -> bool:
        return file_size >= MULTIPART_UPLOADS_MIN_TOTAL_SIZE

    @staticmethod
    def compute_s3_url(*, bucket: S3BucketName, object_key: S3ObjectKey) -> AnyUrl:
        return TypeAdapter(AnyUrl).validate_python(
            f"s3://{bucket}/{urllib.parse.quote(object_key)}"
        )
