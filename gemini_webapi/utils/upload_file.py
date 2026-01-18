from pathlib import Path

from httpx import AsyncClient, Cookies

from ..constants import Endpoint, Headers
from .logger import logger


async def upload_file(
    file: str | Path,
    cookies: Cookies | dict,
    proxy: str | None = None,
    debug: bool = False,
) -> str:
    """
    Upload a file to Google's server using the browser's resumable upload protocol.

    Parameters
    ----------
    file : `str` | `Path`
        Path to the file to be uploaded.
    cookies : `Cookies` | `dict`
        Authentication cookies from the Gemini client.
    proxy : `str`, optional
        Proxy URL.
    debug : `bool`, optional
        Enable debug logging.

    Returns
    -------
    `str`
        Identifier of the uploaded file.

    Raises
    ------
    `httpx.HTTPStatusError`
        If the upload request failed.
    `ValueError`
        If the file is invalid or upload fails.
    """

    file_path = Path(file)
    if not file_path.is_file():
        raise ValueError(f"{file_path} is not a valid file.")

    filename = file_path.name
    file_size = file_path.stat().st_size

    with open(file_path, "rb") as f:
        file_content = f.read()

    if debug:
        logger.debug(f"[Upload] Starting upload for: {filename} ({file_size} bytes)")

    # Headers for Step 1: Start upload session
    # Combine values from constants to avoid hardcoding
    start_headers = {
        "Content-Type": Headers.GEMINI.value["Content-Type"],
        "Origin": Headers.GEMINI.value["Origin"],
        "Referer": Headers.GEMINI.value["Referer"],
        "User-Agent": Headers.GEMINI.value["User-Agent"],
        "Push-ID": Headers.UPLOAD.value["Push-ID"],
        "x-goog-upload-command": "start",
        "x-goog-upload-header-content-length": str(file_size),
        "x-goog-upload-protocol": Headers.UPLOAD.value["x-goog-upload-protocol"],
        "x-tenant-id": Headers.UPLOAD.value["x-tenant-id"],
        # Browser headers from constants
        **Headers.BROWSER.value,
    }

    async with AsyncClient(proxy=proxy, http2=True, follow_redirects=True) as client:
        # ============================================================
        # Step 1: Start upload session
        # ============================================================
        if debug:
            logger.debug(f"[Upload] Step 1: Starting upload session...")
            logger.debug(f"[Upload] URL: {Endpoint.UPLOAD.value}")
            logger.debug(f"[Upload] Headers: {start_headers}")

        start_response = await client.post(
            url=Endpoint.UPLOAD.value,
            headers=start_headers,
            cookies=cookies,
            content=f"File name: {filename}",
        )

        if debug:
            logger.debug(f"[Upload] Step 1 Response Status: {start_response.status_code}")
            logger.debug(f"[Upload] Step 1 Response Headers: {dict(start_response.headers)}")

        start_response.raise_for_status()

        # Extract upload URL from response headers
        upload_url = start_response.headers.get("x-goog-upload-url")
        if not upload_url:
            # Fallback: check if it's in the response body or a different header
            if debug:
                logger.debug(f"[Upload] Step 1 Response Body: {start_response.text[:500]}")
            raise ValueError("Failed to get upload URL from start response. Missing 'x-goog-upload-url' header.")

        if debug:
            logger.debug(f"[Upload] Got upload URL: {upload_url}")

        # ============================================================
        # Step 2: Upload binary data and finalize
        # ============================================================
        upload_headers = {
            "Content-Type": Headers.GEMINI.value["Content-Type"],
            "Origin": Headers.GEMINI.value["Origin"],
            "Referer": Headers.GEMINI.value["Referer"],
            "User-Agent": Headers.GEMINI.value["User-Agent"],
            "Push-ID": Headers.UPLOAD.value["Push-ID"],
            "x-goog-upload-command": "upload, finalize",
            "x-goog-upload-offset": "0",
            "x-tenant-id": Headers.UPLOAD.value["x-tenant-id"],
            # Browser headers from constants
            **Headers.BROWSER.value,
        }

        if debug:
            logger.debug(f"[Upload] Step 2: Uploading binary data...")
            logger.debug(f"[Upload] URL: {upload_url}")
            logger.debug(f"[Upload] Content size: {len(file_content)} bytes")

        upload_response = await client.post(
            url=upload_url,
            headers=upload_headers,
            cookies=cookies,
            content=file_content,  # Raw binary data
        )

        if debug:
            logger.debug(f"[Upload] Step 2 Response Status: {upload_response.status_code}")
            logger.debug(f"[Upload] Step 2 Response: {upload_response.text[:500] if upload_response.text else '(empty)'}")

        upload_response.raise_for_status()

        file_id = upload_response.text.strip()
        if debug:
            logger.debug(f"[Upload] Upload complete! File ID: {file_id}")

        return file_id


def parse_file_name(file: str | Path) -> str:
    """
    Parse the file name from the given path.

    Parameters
    ----------
    file : `str` | `Path`
        Path to the file.

    Returns
    -------
    `str`
        File name with extension.
    """

    file = Path(file)
    if not file.is_file():
        raise ValueError(f"{file} is not a valid file.")

    return file.name
