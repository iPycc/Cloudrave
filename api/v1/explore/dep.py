"""Explorer route service builders."""

from services.explorer.download import DownloadFileService
from services.explorer.file import FileExplorerService
from services.explorer.preview import PreviewService
from services.explorer.upload import CreateUploadSessionService


def node_service(dep, session) -> FileExplorerService:
    return FileExplorerService(
        session=session,
        mounts=dep.get_mount_repository(session),
        policies=dep.get_policy_repository(session),
        nodes=dep.get_node_repository(session),
        objects=dep.get_object_repository(session),
        audits=dep.get_audit_repository(session),
        storage_manager=dep.storage_manager,
    )


def upload_service(dep, session) -> CreateUploadSessionService:
    return CreateUploadSessionService(
        session=session,
        mounts=dep.get_mount_repository(session),
        policies=dep.get_policy_repository(session),
        nodes=dep.get_node_repository(session),
        objects=dep.get_object_repository(session),
        uploads=dep.get_upload_repository(session),
        audits=dep.get_audit_repository(session),
        storage_manager=dep.storage_manager,
    )


def download_service(dep, session) -> DownloadFileService:
    return DownloadFileService(
        mounts=dep.get_mount_repository(session),
        nodes=dep.get_node_repository(session),
        policies=dep.get_policy_repository(session),
        objects=dep.get_object_repository(session),
        storage_manager=dep.storage_manager,
    )


def preview_service(dep, session) -> PreviewService:
    resolver = download_service(dep, session)
    return PreviewService(
        resolver=resolver,
        nodes=dep.get_node_repository(session),
        objects=dep.get_object_repository(session),
        audits=dep.get_audit_repository(session),
        annotations=dep.get_preview_annotation_repository(session),
        storage_manager=dep.storage_manager,
        token_service=dep.token_service,
        cache_root=dep.config.preview_cache_root_path,
        public_base_url=dep.config.app.public_base_url,
        office_ttl_seconds=dep.config.app.office_source_ttl_seconds,
        text_max_bytes=dep.config.app.text_preview_max_bytes,
    )


_build_preview_service = preview_service
