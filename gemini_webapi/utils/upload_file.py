from pathlib import Path

from httpx import AsyncClient

from ..constants import Endpoint, Headers


async def upload_file(
    file: str | Path,
    proxy: str | None = None,
) -> str:
    """
    Upload a file to Google's server and return its identifier.

    Parameters
    ----------
    file : `str` | `Path`
        Path to the file to be uploaded.
    proxy: `str`, optional
        Proxy URL.

    Returns
    -------
    `str`
        Identifier of the uploaded file.
        E.g. "/contrib_service/ttl_1d/1709764705i7wdlyx3mdzndme3a767pluckv4flj"

    Raises
    ------
    `httpx.HTTPStatusError`
        If the upload request failed.
    """

    file_path = Path(file)
    if not file_path.is_file():
        raise ValueError(f"{file_path} is not a valid file.")

    filename = file_path.name

    with open(file_path, "rb") as f:
        file_content = f.read()

    # Upload needs its own client with clean headers (not Gemini headers)
    async with AsyncClient(proxy=proxy, http2=True) as client:
        response = await client.post(
            url=Endpoint.UPLOAD.value,
            headers=Headers.UPLOAD.value,
            files={"file": (filename, file_content)},
            follow_redirects=True,
        )
        response.raise_for_status()
        return response.text


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
