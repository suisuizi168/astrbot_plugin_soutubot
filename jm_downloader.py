"""JM service facade; image transport and shared PDF lifecycle are separate."""
if __package__:
    from .download_service import (
        DownloadService, DownloadError as JmDownloadError,
        AlbumNotFoundError as JmAlbumNotFoundError,
        PartialDownloadError as JmPartialDownloadError,
        DownloadTimeoutError as JmDownloadTimeoutError,
        PdfGenerationError as JmPdfGenerationError,
        DownloadArtifact as JmDownloadArtifact,
        DownloadAcquireResult as JmDownloadAcquireResult,
        CleanupResult as JmCleanupResult,
    )
    from .jm_source import JmImageSource, JmDownloadSettings, normalize_jm_id
else:
    from download_service import (
        DownloadService, DownloadError as JmDownloadError,
        AlbumNotFoundError as JmAlbumNotFoundError,
        PartialDownloadError as JmPartialDownloadError,
        DownloadTimeoutError as JmDownloadTimeoutError,
        PdfGenerationError as JmPdfGenerationError,
        DownloadArtifact as JmDownloadArtifact,
        DownloadAcquireResult as JmDownloadAcquireResult,
        CleanupResult as JmCleanupResult,
    )
    from jm_source import JmImageSource, JmDownloadSettings, normalize_jm_id

class JmDownloadService(DownloadService):
    def __init__(self, settings, *, downloader=None, option_factory=None, adapter=None, pdf_service=None):
        adapter = adapter or JmImageSource(settings, downloader=downloader, option_factory=option_factory)
        super().__init__(settings, adapter, pdf_service=pdf_service)
