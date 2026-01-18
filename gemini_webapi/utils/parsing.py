from typing import Any

import orjson as json


def get_nested_value(data: list, path: list[int], default: Any = None) -> Any:
    """
    Safely get a value from a nested list by a sequence of indices.

    Parameters
    ----------
    data: `list`
        The nested list to traverse.
    path: `list[int]`
        A list of indices representing the path to the desired value.
    default: `Any`, optional
        The default value to return if the path is not found.
    """

    current = data

    for i, key in enumerate(path):
        try:
            current = current[key]
        except (IndexError, TypeError, KeyError):
            # Silently return default - this is normal for optional paths
            return default

    if current is None and default is not None:
        return default

    return current


def extract_json_from_response(text: str) -> list:
    """
    Clean and extract JSON content from a Google API streaming response.
    
    The response contains multiple JSON chunks prefixed by byte counts (e.g., "332\n[...]").
    This function collects ALL valid JSON arrays from the response, not just the first one.

    Parameters
    ----------
    text: `str`
        The raw response text from a Google API.

    Returns
    -------
    `list`
        A list containing all parsed JSON arrays from the streaming response.

    Raises
    ------
    `TypeError`
        If the input is not a string.
    `ValueError`
        If no JSON object is found or the response is empty.
    """

    if not isinstance(text, str):
        raise TypeError(
            f"Input text is expected to be a string, got {type(text).__name__} instead."
        )

    # Collect ALL valid JSON arrays from the streaming response
    all_chunks = []
    
    for line in text.splitlines():
        stripped = line.strip()
        # Skip empty lines, byte counts (pure digits), and the )]}' prefix
        if not stripped or stripped.isdigit() or stripped == ")]}'":
            continue
        try:
            parsed = json.loads(stripped)
            # Only add if it's a list (valid response chunk)
            if isinstance(parsed, list):
                all_chunks.append(parsed)
        except json.JSONDecodeError:
            continue

    if not all_chunks:
        raise ValueError("Could not find a valid JSON object or array in the response.")
    
    return all_chunks
