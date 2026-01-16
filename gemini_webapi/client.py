import asyncio
import re
from asyncio import Task
from pathlib import Path
from typing import Any, Optional

import orjson as json
from httpx import AsyncClient, ReadTimeout, Response

from .components import GemMixin
from .constants import Endpoint, ErrorCode, GRPC, Headers, ImageStatus, Model
from .exceptions import (
    APIError,
    AuthError,
    GeminiError,
    ImageGenerationError,
    ModelInvalid,
    TemporarilyBlocked,
    TimeoutError,
    UsageLimitExceeded,
)
from .types import (
    Candidate,
    Gem,
    GeneratedImage,
    ModelOutput,
    RPCData,
    WebImage,
)
from .utils import (
    extract_json_from_response,
    get_access_token,
    get_nested_value,
    logger,
    parse_file_name,
    rotate_1psidts,
    rotate_tasks,
    running,
    upload_file,
)


class GeminiClient(GemMixin):
    """
    Async httpx client interface for gemini.google.com.

    `secure_1psid` must be provided unless the optional dependency `browser-cookie3` is installed, and
    you have logged in to google.com in your local browser.

    Parameters
    ----------
    secure_1psid: `str`, optional
        __Secure-1PSID cookie value.
    secure_1psidts: `str`, optional
        __Secure-1PSIDTS cookie value, some google accounts don't require this value, provide only if it's in the cookie list.
    proxy: `str`, optional
        Proxy URL.
    kwargs: `dict`, optional
        Additional arguments which will be passed to the http client.
        Refer to `httpx.AsyncClient` for more information.

    Raises
    ------
    `ValueError`
        If `browser-cookie3` is installed but cookies for google.com are not found in your local browser storage.
    """

    __slots__ = [
        "cookies",
        "proxy",
        "_running",
        "client",
        "access_token",
        "timeout",
        "auto_close",
        "close_delay",
        "close_task",
        "auto_refresh",
        "refresh_interval",
        "_gems",  # From GemMixin
        "kwargs",
    ]

    def __init__(
        self,
        secure_1psid: str | None = None,
        secure_1psidts: str | None = None,
        proxy: str | None = None,
        **kwargs,
    ):
        super().__init__()
        self.cookies = {}
        self.proxy = proxy
        self._running: bool = False
        self.client: AsyncClient | None = None
        self.access_token: str | None = None
        self.timeout: float = 300
        self.auto_close: bool = False
        self.close_delay: float = 300
        self.close_task: Task | None = None
        self.auto_refresh: bool = True
        self.refresh_interval: float = 540
        self.kwargs = kwargs

        if secure_1psid:
            self.cookies["__Secure-1PSID"] = secure_1psid
            if secure_1psidts:
                self.cookies["__Secure-1PSIDTS"] = secure_1psidts

    async def init(
        self,
        timeout: float = 300,
        auto_close: bool = False,
        close_delay: float = 300,
        auto_refresh: bool = True,
        refresh_interval: float = 540,
        verbose: bool = True,
    ) -> None:
        """
        Get SNlM0e value as access token. Without this token posting will fail with 400 bad request.

        Parameters
        ----------
        timeout: `float`, optional
            Request timeout of the client in seconds. Used to limit the max waiting time when sending a request.
        auto_close: `bool`, optional
            If `True`, the client will close connections and clear resource usage after a certain period
            of inactivity. Useful for always-on services.
        close_delay: `float`, optional
            Time to wait before auto-closing the client in seconds. Effective only if `auto_close` is `True`.
        auto_refresh: `bool`, optional
            If `True`, will schedule a task to automatically refresh cookies in the background.
        refresh_interval: `float`, optional
            Time interval for background cookie refresh in seconds. Effective only if `auto_refresh` is `True`.
        verbose: `bool`, optional
            If `True`, will print more infomation in logs.
        """

        try:
            access_token, valid_cookies = await get_access_token(
                base_cookies=self.cookies, proxy=self.proxy, verbose=verbose
            )

            self.client = AsyncClient(
                timeout=timeout,
                proxy=self.proxy,
                follow_redirects=True,
                http2=True,  # Enable HTTP/2 for faster multiplexed requests
                headers={**Headers.GEMINI.value, **Headers.BROWSER.value},
                cookies=valid_cookies,
                **self.kwargs,
            )
            self.access_token = access_token
            self.cookies = valid_cookies
            self._running = True

            self.timeout = timeout
            self.auto_close = auto_close
            self.close_delay = close_delay
            if self.auto_close:
                await self.reset_close_task()

            self.auto_refresh = auto_refresh
            self.refresh_interval = refresh_interval
            if task := rotate_tasks.get(self.cookies["__Secure-1PSID"]):
                task.cancel()
            if self.auto_refresh:
                rotate_tasks[self.cookies["__Secure-1PSID"]] = asyncio.create_task(
                    self.start_auto_refresh()
                )

            if verbose:
                logger.success("Gemini client initialized successfully.")
        except Exception:
            await self.close()
            raise

    async def close(self, delay: float = 0) -> None:
        """
        Close the client after a certain period of inactivity, or call manually to close immediately.

        Parameters
        ----------
        delay: `float`, optional
            Time to wait before closing the client in seconds.
        """

        if delay:
            await asyncio.sleep(delay)

        self._running = False

        if self.close_task:
            self.close_task.cancel()
            self.close_task = None

        if self.client:
            await self.client.aclose()

    async def reset_close_task(self) -> None:
        """
        Reset the timer for closing the client when a new request is made.
        """

        if self.close_task:
            self.close_task.cancel()
            self.close_task = None

        self.close_task = asyncio.create_task(self.close(self.close_delay))

    async def start_auto_refresh(self) -> None:
        """
        Start the background task to automatically refresh cookies.
        """

        while True:
            new_1psidts: str | None = None
            try:
                new_1psidts = await rotate_1psidts(self.cookies, self.proxy)
            except AuthError:
                if task := rotate_tasks.get(self.cookies.get("__Secure-1PSID", "")):
                    task.cancel()
                logger.warning(
                    "AuthError: Failed to refresh cookies. Auto refresh task canceled."
                )
                return
            except Exception as exc:
                logger.warning(f"Unexpected error while refreshing cookies: {exc}")

            if new_1psidts:
                self.cookies["__Secure-1PSIDTS"] = new_1psidts
                if self._running:
                    self.client.cookies.set("__Secure-1PSIDTS", new_1psidts)
                logger.debug("Cookies refreshed. New __Secure-1PSIDTS applied.")

            await asyncio.sleep(self.refresh_interval)

    @running(retry=2)
    async def generate_content(
        self,
        prompt: str,
        files: list[str | Path] | None = None,
        model: Model | str | dict = Model.UNSPECIFIED,
        gem: Gem | str | None = None,
        chat: Optional["ChatSession"] = None,
        image_mode: bool = False,
        debug_mode: bool = False,
        **kwargs,
    ) -> ModelOutput:
        """
        Generates contents with prompt.

        Parameters
        ----------
        prompt: `str`
            Prompt provided by user.
        files: `list[str | Path]`, optional
            List of file paths to be attached.
        model: `Model | str | dict`, optional
            Specify the model to use for generation.
            Pass either a `gemini_webapi.constants.Model` enum or a model name string to use predefined models.
            Pass a dictionary to use custom model header strings ("model_name" and "model_header" keys must be provided).
        gem: `Gem | str`, optional
            Specify a gem to use as system prompt for the chat session.
            Pass either a `gemini_webapi.types.Gem` object or a gem id string.
        chat: `ChatSession`, optional
            Chat data to retrieve conversation history. If None, will automatically generate a new chat id when sending post request.
        kwargs: `dict`, optional
            Additional arguments which will be passed to the post request.
            Refer to `httpx.AsyncClient.request` for more information.

        Returns
        -------
        :class:`ModelOutput`
            Output data from gemini.google.com, use `ModelOutput.text` to get the default text reply, `ModelOutput.images` to get a list
            of images in the default reply, `ModelOutput.candidates` to get a list of all answer candidates in the output.

        Raises
        ------
        `AssertionError`
            If prompt is empty.
        `gemini_webapi.TimeoutError`
            If request timed out.
        `gemini_webapi.GeminiError`
            If no reply candidate found in response.
        `gemini_webapi.APIError`
            - If request failed with status code other than 200.
            - If response structure is invalid and failed to parse.
        """

        assert prompt, "Prompt cannot be empty."

        if isinstance(model, str):
            model = Model.from_name(model)
        elif isinstance(model, dict):
            model = Model.from_dict(model)
        elif not isinstance(model, Model):
            raise TypeError(
                f"'model' must be a `gemini_webapi.constants.Model` instance, "
                f"string, or dictionary; got `{type(model).__name__}`"
            )

        if isinstance(gem, Gem):
            gem_id = gem.id
        else:
            gem_id = gem

        if self.auto_close:
            await self.reset_close_task()

        # Build final headers
        final_headers = {}
        
        # Setup image mode if requested
        if image_mode:
            import uuid
            session_uuid = str(uuid.uuid4()).upper()
            
            # Image mode has two sub-cases:
            # 1. Text-to-image (no files): Use extended headers with model ID and session UUID
            # 2. Image-to-image (with files): Use standard model headers only
            #    The extended headers cause i2i requests to return empty responses
            
            if files:
                # Image-to-Image: Use standard model headers (matches upstream Gemini-API)
                logger.debug(f"Image mode (i2i): Using standard model headers for editing")
                if model.model_header:
                    final_headers = {**model.model_header}
            else:
                # Text-to-Image: Use extended headers with model ID and session UUID
                # Image mode REQUIRES a valid model ID - default to gemini-3-flash if unspecified
                model_header_str = model.model_header.get("x-goog-ext-525001261-jspb", "")
                if '"' in model_header_str:
                    # Extract model ID from existing header
                    model_id = model_header_str.split('"')[1]
                else:
                    # Default to gemini-3-flash model ID for image mode
                    model_id = "9ec249fc9ad08861"  # G_2_5_FLASH model ID
                
                final_headers["x-goog-ext-525001261-jspb"] = f'[1,null,null,null,"{model_id}",null,null,0,[4],null,null,2]'
                final_headers["x-goog-ext-525005358-jspb"] = f'["{session_uuid}",1]'
                # Note: x-goog-ext-73010989-jspb is now included in base GEMINI headers
                
                logger.debug(f"Image mode (t2i): model_name='{model.model_name}', model_id='{model_id}'")
        else:
            # Non-image mode: use model headers if specified
            session_uuid = None  # No session UUID needed for text mode
            if model.model_header:
                final_headers = {**model.model_header}


        try:
            # Helper function to build payload array with specific values at specific positions
            def build_payload(size: int, values: dict) -> list:
                """Build an array of given size with values at specific positions."""
                payload = [None] * size
                for pos, val in values.items():
                    payload[pos] = val
                return payload
            
            # Build prompt data for position 0
            if files:
                prompt_data = [
                    prompt,
                    0,
                    None,
                    [
                        [
                            [await upload_file(file, self.proxy)],
                            parse_file_name(file),
                        ]
                        for file in files
                    ],
                    None,
                    None,
                    0,
                ]
            else:
                prompt_data = [prompt, 0, None, None, None, None, 0]
            
            # Chat metadata for position 2
            chat_metadata = chat.metadata if chat else ["", "", "", None, None, None, None, None, None, ""]
            
            if image_mode:
                # Image mode has two sub-cases:
                # 1. Text-to-image (no files): Use 67-element array with image generation flags
                # 2. Image-to-image (with files): Use simpler structure matching upstream Gemini-API
                #    The complex array structure breaks when files are attached
                
                if files:
                    # Image-to-Image: Use simpler payload structure (matches upstream Gemini-API)
                    # This format works for editing images with prompts
                    logger.debug(f"Image mode (i2i): Using simple payload with {len(files)} file(s)")
                    inner_payload = [
                        prompt_data,  # [prompt, 0, None, [[file_data], filename], None, None, 0]
                        None,
                        chat_metadata,
                    ]
                    # Append gem_id if provided
                    if gem_id:
                        inner_payload.extend([None] * 16 + [gem_id])
                else:
                    # Text-to-Image: Use 67-element array with image generation flags
                    # Payload Structure Documentation:
                    #   [0]  = prompt_data: User prompt with optional file attachments
                    #   [1]  = ["en"]: Language setting
                    #   [2]  = chat_metadata: [cid, rid, rcid, ...] for conversation continuity
                    #   [7]  = 1: Enable image generation mode
                    #   [10] = 1: Secondary image mode flag
                    #   [17] = [[0]]: Image mode indicator array
                    #   [27] = 1: Required flag for image processing
                    #   [30] = [4]: Image format preference (4 = standard)
                    #   [41] = [1]: Required processing flag
                    #   [49] = 14: Image generation operation type
                    #   [59] = session_uuid: Unique session identifier
                    #   [66] = [timestamp_sec, timestamp_ns]: Request timestamp
                    import time
                    current_time = int(time.time())
                    timestamp_ns = int((time.time() % 1) * 1000000000)
                    
                    logger.debug(f"Image mode (t2i): Using 67-element payload for generation")
                    image_mode_values = {
                        0: prompt_data,
                        1: ["en"],
                        2: chat_metadata,
                        7: 1,
                        10: 1,
                        17: [[0]],
                        27: 1,
                        30: [4],
                        41: [1],
                        49: 14,
                        59: session_uuid,
                        66: [current_time, timestamp_ns],
                    }
                    inner_payload = build_payload(67, image_mode_values)
            else:
                # Text mode: Simpler payload structure
                text_mode_values = {
                    0: prompt_data,
                    1: ["en"],
                    2: chat_metadata,
                }
                inner_payload = build_payload(20, text_mode_values)
                
                # Append gem_id if provided
                if gem_id:
                    inner_payload.extend([None] * 16 + [gem_id])
            
            response = await self.client.post(
                Endpoint.GENERATE.value,
                headers=final_headers,
                data={
                    "at": self.access_token,
                    "f.req": json.dumps(
                        [
                            None,
                            json.dumps(inner_payload).decode(),
                        ]
                    ).decode(),
                },
                **kwargs,
            )
        except ReadTimeout:
            raise TimeoutError(
                "Generate content request timed out, please try again. If the problem persists, "
                "consider setting a higher `timeout` value when initializing GeminiClient."
            )

        if response.status_code != 200:
            await self.close()
            raise APIError(
                f"Failed to generate contents. Request failed with status code {response.status_code}"
            )
        else:
            # Debug: Save request and response to files if debug_mode is enabled
            if debug_mode:
                import os
                debug_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                
                try:
                    request_data = {
                        "prompt": prompt,
                        "files": [str(f) for f in files] if files else None,
                        "image_mode": image_mode,
                        "model": model.model_name if hasattr(model, 'model_name') else str(model),
                    }
                    with open(os.path.join(debug_dir, "debug_request.txt"), "w", encoding="utf-8") as f:
                        f.write(json.dumps(request_data, option=json.OPT_INDENT_2).decode())
                    print(f"[Gemini Debug] Saved request to {debug_dir}/debug_request.txt")
                except Exception as e:
                    logger.debug(f"Failed to save request: {e}")
                
                try:
                    # Sanitize response to remove sensitive location/Google Maps data
                    def sanitize_response(text):
                        """Remove sensitive location data from response for safe sharing."""
                        import re
                        sanitized = text
                        
                        # Remove Google Maps URLs
                        sanitized = re.sub(
                            r'//www\.google\.com/maps/vt/data=[^"\']+',
                            '[MAPS_DATA_REDACTED]',
                            sanitized
                        )
                        
                        # Remove location descriptions (city, state, country patterns)
                        # Matches: "City Name, Area, City, State, Country"
                        sanitized = re.sub(
                            r'"[A-Z][a-zA-Z\s]+,\s*[A-Z][a-zA-Z\s]+,\s*[A-Z][a-zA-Z\s]+,\s*[A-Z][a-zA-Z\s]+,\s*[A-Z][a-zA-Z\s]+"',
                            '"[LOCATION_REDACTED]"',
                            sanitized
                        )
                        
                        # Remove SWML location markers
                        sanitized = re.sub(
                            r'"SWML_DESCRIPTION_FROM_YOUR_PLACES_[A-Z]+"',
                            '"[PLACES_MARKER_REDACTED]"',
                            sanitized
                        )
                        
                        # Remove coordinates if present (lat, lng patterns)
                        sanitized = re.sub(
                            r'(\d+\.\d{4,}),\s*(\d+\.\d{4,})',
                            '[COORDS_REDACTED]',
                            sanitized
                        )
                        
                        return sanitized
                    
                    def parse_to_clean_json(raw_text):
                        """Parse Google XSSI-protected response into clean JSON."""
                        import re
                        
                        # Extract JSON chunks from XSSI-protected response
                        parsed_chunks = []
                        try:
                            # Use the existing extract_json_from_response function
                            chunks = extract_json_from_response(raw_text)
                            
                            for i, chunk in enumerate(chunks):
                                chunk_data = {"chunk_index": i, "data": None}
                                try:
                                    # Try to get the inner body and parse nested JSON strings
                                    if isinstance(chunk, list) and len(chunk) > 2 and chunk[2]:
                                        inner_json_str = chunk[2]
                                        if isinstance(inner_json_str, str):
                                            inner_data = json.loads(inner_json_str)
                                            chunk_data["data"] = inner_data
                                        else:
                                            chunk_data["data"] = chunk
                                    else:
                                        chunk_data["data"] = chunk
                                except (json.JSONDecodeError, TypeError):
                                    chunk_data["data"] = chunk
                                
                                parsed_chunks.append(chunk_data)
                        except Exception as e:
                            # Fallback: return raw text info
                            parsed_chunks = [{"error": str(e), "raw_length": len(raw_text)}]
                        
                        return parsed_chunks
                    
                    # Parse to clean JSON structure
                    clean_json = parse_to_clean_json(response.text)
                    
                    # Apply sanitization to the formatted output
                    formatted_output = json.dumps(clean_json, option=json.OPT_INDENT_2).decode()
                    sanitized_output = sanitize_response(formatted_output)
                    
                    with open(os.path.join(debug_dir, "debug_response.json"), "w", encoding="utf-8") as f:
                        f.write(sanitized_output)
                    print(f"[Gemini Debug] Saved parsed response to {debug_dir}/debug_response.json (location data redacted)")
                    
                    # Also save raw response for complete debugging (sanitized)
                    sanitized_raw = sanitize_response(response.text)
                    with open(os.path.join(debug_dir, "debug_response_raw.txt"), "w", encoding="utf-8") as f:
                        f.write(sanitized_raw)
                    print(f"[Gemini Debug] Saved raw response to {debug_dir}/debug_response_raw.txt")
                except Exception as e:
                    logger.debug(f"Failed to save response: {e}")
            
            response_json: list[Any] = []

            body: list[Any] = []
            body_index = 0

            try:
                response_json = extract_json_from_response(response.text)

                # Find the best body - look for the chunk with actual text content
                # Streaming responses have multiple chunks; early ones may have empty text
                best_body = None
                best_body_index = 0
                
                for part_index, part in enumerate(response_json):
                    try:
                        part_body = get_nested_value(part, [2])
                        if not part_body:
                            continue

                        part_json = json.loads(part_body)
                        candidates = get_nested_value(part_json, [4])
                        if candidates:
                            # Check if this chunk has actual text content
                            text_content = get_nested_value(candidates, [0, 1, 0], "")
                            
                            if debug_mode:
                                logger.debug(f"Part {part_index}: text_content = '{text_content[:50] if text_content else 'None'}...'")
                            
                            # Prefer chunks with text, but keep any valid candidate as fallback
                            if text_content and text_content.strip():
                                best_body = part_json
                                best_body_index = part_index
                            elif best_body is None:
                                best_body = part_json
                                best_body_index = part_index
                    except json.JSONDecodeError:
                        continue

                if debug_mode:
                    final_text = get_nested_value(best_body, [4, 0, 1, 0], "") if best_body else "None"
                    logger.debug(f"Selected body index {best_body_index}, text: '{final_text[:100] if final_text else 'None'}...'")
                
                body = best_body if best_body else []
                body_index = best_body_index

                if not body:
                    raise Exception
            except Exception:
                await self.close()

                try:
                    # First check for batchexecute safety filter responses
                    # Safety filter returns [3] or [4] at index [0][5]
                    safety_code = get_nested_value(response_json, [0, 5], None)
                    if safety_code == [3] or safety_code == [4] or safety_code == 3 or safety_code == 4:
                        raise GeminiError(
                            "Failed to generate contents. Your prompt was rejected by Gemini's content safety filters. "
                            "Please try a different prompt that doesn't trigger content moderation."
                        )
                    
                    # Check for BardErrorInfo response (code [5,...] at index [0][5])
                    # This indicates various API errors including image session issues (1115)
                    if isinstance(safety_code, list) and len(safety_code) >= 1 and safety_code[0] == 5:
                        # Extract error code from BardErrorInfo if present
                        bard_error_code = get_nested_value(response_json, [0, 5, 2, 0, 1, 0], -1)
                        if bard_error_code == 1115:
                            raise APIError(
                                "Failed to generate image. Image generation session initialization failed (Error 1115). "
                                "This may be a temporary issue - please try again or check your cookies."
                            )
                        else:
                            logger.debug(f"BardErrorInfo with code: {bard_error_code}")
                            raise APIError(
                                f"Failed to generate contents. Gemini returned error code {bard_error_code}. "
                                "Please try again or check your authentication."
                            )
                    
                    # Then check for standard error codes
                    error_code = get_nested_value(response_json, [0, 5, 2, 0, 1, 0], -1)
                    match error_code:
                        case ErrorCode.CONTENT_SAFETY_3 | ErrorCode.CONTENT_SAFETY_4:
                            raise GeminiError(
                                "Failed to generate contents. Your prompt was rejected by Gemini's content safety filters. "
                                "Please try a different prompt that doesn't trigger content moderation."
                            )
                        case ErrorCode.USAGE_LIMIT_EXCEEDED:
                            raise UsageLimitExceeded(
                                f"Failed to generate contents. Usage limit of {model.model_name} model has exceeded. Please try switching to another model."
                            )
                        case ErrorCode.MODEL_INCONSISTENT:
                            raise ModelInvalid(
                                "Failed to generate contents. The specified model is inconsistent with the chat history. Please make sure to pass the same "
                                "`model` parameter when starting a chat session with previous metadata."
                            )
                        case ErrorCode.MODEL_HEADER_INVALID:
                            raise ModelInvalid(
                                "Failed to generate contents. The specified model is not available. Please update gemini_webapi to the latest version. "
                                "If the error persists and is caused by the package, please report it on GitHub."
                            )
                        case ErrorCode.IP_TEMPORARILY_BLOCKED:
                            raise TemporarilyBlocked(
                                "Failed to generate contents. Your IP address is temporarily blocked by Google. Please try using a proxy or waiting for a while."
                            )
                        case _:
                            raise Exception
                except GeminiError:
                    raise
                except Exception:
                    logger.debug(f"Invalid response: {response.text}")
                    raise APIError(
                        "Failed to generate contents. Invalid response data received. Client will try to re-initialize on next request."
                    )

            try:
                candidate_list: list[Any] = get_nested_value(body, [4], [])
                output_candidates: list[Candidate] = []

                for candidate_index, candidate in enumerate(candidate_list):
                    rcid = get_nested_value(candidate, [0])
                    if not rcid:
                        continue  # Skip candidate if it has no rcid

                    # Text output and thoughts
                    text = get_nested_value(candidate, [1, 0], "")
                    if re.match(
                        r"^http://googleusercontent\.com/card_content/\d+", text
                    ):
                        text = get_nested_value(candidate, [22, 0]) or text

                    thoughts = get_nested_value(candidate, [37, 0, 0])

                    # Web images
                    web_images = []
                    for web_img_data in get_nested_value(candidate, [12, 1], []):
                        url = get_nested_value(web_img_data, [0, 0, 0])
                        if not url:
                            continue

                        web_images.append(
                            WebImage(
                                url=url,
                                title=get_nested_value(web_img_data, [7, 0], ""),
                                alt=get_nested_value(web_img_data, [0, 4], ""),
                                proxy=self.proxy,
                            )
                        )

                    # =====================================================
                    # ROBUST IMAGE EXTRACTION (v2 - Anti-Fragility Design)
                    # =====================================================
                    # Implements: Score-based chunk selection, recursive blob finder,
                    # hunt-and-peck index verification, priority-based extraction,
                    # MIME-based watermark detection, safety handling, early exit
                    # =====================================================
                    
                    generated_images = []
                    
                    # Helper: Recursive image blob finder
                    def find_image_blobs(node, depth=0, max_depth=12, input_urls=None, parent_index=None):
                        """
                        Recursively find all image blob arrays in a nested structure.
                        Image blob pattern: [null/any, 1, "filename", "https://...googleusercontent...", ...]
                        
                        Watermark detection is based on parent array position:
                        - Position [3] = watermarked (first image)
                        - Position [6] = no watermark (second image)
                        """
                        if input_urls is None:
                            input_urls = set()
                        
                        results = []
                        if depth > max_depth or not isinstance(node, list):
                            return results
                        
                        # Check if this node is an image blob
                        if len(node) >= 4:
                            url = node[3] if len(node) > 3 else None
                            if isinstance(url, str) and "googleusercontent.com" in url:
                                filename = node[2] if len(node) > 2 and isinstance(node[2], str) else ""
                                mime_type = node[9] if len(node) > 9 and isinstance(node[9], str) else ""
                                
                                # Classify as generated or input
                                is_generated = False
                                is_input = False
                                
                                # Primary: URL pattern check
                                if "/gg-dl/" in url:
                                    is_generated = True
                                elif "googleusercontent.com/gg/" in url and "/gg-dl/" not in url:
                                    is_input = True
                                
                                # Secondary: Filename entropy (generated have long numeric names)
                                if not is_generated and not is_input:
                                    if filename and len(filename) > 20 and filename[0].isdigit():
                                        is_generated = True
                                
                                # Tertiary: URL prefix patterns
                                if not is_generated and not is_input:
                                    if "ABS2G" in url:
                                        is_generated = True
                                    elif "AIJ2g" in url:
                                        is_input = True
                                
                                # Final safety: Exclude exact input URLs
                                if url in input_urls:
                                    is_input = True
                                    is_generated = False
                                
                                if is_generated:
                                    # Determine watermark status from parent array position
                                    # Position [3] = watermarked, Position [6] = no watermark
                                    # This is based on Gemini's response structure where images are at:
                                    # [[...[BLOB_AT_3], null, null, [BLOB_AT_6]...]]
                                    is_watermarked = parent_index == 3 if parent_index is not None else True
                                    
                                    results.append({
                                        "url": url,
                                        "filename": filename,
                                        "mime_type": mime_type,
                                        "is_watermarked": is_watermarked,
                                        "parent_index": parent_index,
                                        "depth": depth,
                                    })
                        
                        # Recurse into children, tracking their index in the parent array
                        for idx, child in enumerate(node):
                            if isinstance(child, list):
                                results.extend(find_image_blobs(child, depth + 1, max_depth, input_urls, parent_index=idx))
                        
                        return results
                    
                    # Helper: Find image container with hunt-and-peck verification
                    def find_image_container(cand, expected_index=12):
                        """
                        Find the image data container, using hunt-and-peck if the expected index is wrong.
                        Returns the container and its actual index.
                        """
                        # Try expected index first
                        container = get_nested_value(cand, [expected_index])
                        if verify_image_container(container):
                            return container, expected_index
                        
                        # Hunt in neighboring indices
                        for i in range(max(0, expected_index - 3), min(len(cand) if isinstance(cand, list) else 20, expected_index + 4)):
                            if i == expected_index:
                                continue
                            container = get_nested_value(cand, [i])
                            if verify_image_container(container):
                                if debug_mode:
                                    logger.debug(f"Image container found at [{i}] instead of [{expected_index}]")
                                return container, i
                        
                        return None, -1
                    
                    def verify_image_container(container):
                        """Verify this is an image container by checking for expected structure."""
                        if not isinstance(container, list) or len(container) < 6:
                            return False
                        # Check for image mode indicators at [6] or [7]
                        has_status = isinstance(get_nested_value(container, [6]), list)
                        has_img_data = get_nested_value(container, [7]) is not None
                        return has_status or has_img_data
                    
                    # Helper: Score a chunk for image content
                    def score_chunk(part_json, cand_index, input_urls):
                        """
                        Score a response chunk for image content quality.
                        Higher score = better candidate for image extraction.
                        """
                        score = 0
                        part_cand = get_nested_value(part_json, [4, cand_index])
                        if not part_cand:
                            return score, None
                        
                        # Find image container
                        img_container, _ = find_image_container(part_cand)
                        if img_container:
                            score += 10
                        
                        # Check status
                        status_arr = get_nested_value(img_container, [6]) if img_container else None
                        status = status_arr[0] if isinstance(status_arr, list) and status_arr else -1
                        
                        if isinstance(status, int):
                            if status >= ImageStatus.IN_PROGRESS:
                                score += 5
                            if status == ImageStatus.COMPLETE:
                                score += 2
                            # Safety/blocked check
                            if status in [ImageStatus.SAFETY_BLOCKED, ImageStatus.ERROR]:
                                return -100, part_cand  # Blocked - negative score
                        
                        # Check for image data at primary path [7]
                        primary_data = get_nested_value(img_container, [7, 0]) if img_container else None
                        if primary_data:
                            score += 20
                        
                        # Check for /gg-dl/ URLs (strongest signal)
                        blobs = find_image_blobs(img_container, input_urls=input_urls) if img_container else []
                        if blobs:
                            score += 30
                        
                        return score, part_cand
                    
                    # Check if image mode is active
                    img_container, img_container_idx = find_image_container(candidate)
                    is_image_mode = img_container is not None
                    
                    if is_image_mode:
                        status_arr = get_nested_value(img_container, [6])
                        img_mode_status = status_arr[0] if isinstance(status_arr, list) and status_arr else None
                        
                        if debug_mode:
                            logger.debug(f"Image mode detected at [{img_container_idx}][6] = {status_arr}, status={img_mode_status}")
                        
                        # Check for safety/blocked status
                        if isinstance(img_mode_status, int) and img_mode_status in [ImageStatus.SAFETY_BLOCKED, ImageStatus.ERROR]:
                            if debug_mode:
                                logger.debug(f"Image generation BLOCKED (status={img_mode_status})")
                            # Don't raise - just set empty images and continue
                            output_candidates.append(
                                Candidate(
                                    rcid=rcid,
                                    text="Image generation was blocked by safety filters.",
                                    thoughts=thoughts,
                                    web_images=web_images,
                                    generated_images=[],
                                )
                            )
                            continue
                        
                        # Collect input image URLs for exclusion
                        input_urls = set()
                        # Input images are often at [12][7][0] with /gg/ pattern in earlier chunks
                        # We'll detect them by /gg/ without /gg-dl/
                        
                        # SCORE-BASED CHUNK SELECTION with early exit
                        best_score = -1
                        best_candidate = None
                        best_chunk_idx = -1
                        
                        for part_idx, part in enumerate(response_json):
                            try:
                                # EARLY EXIT: Skip chunks with status < 2 (JSON peek optimization)
                                raw_body = get_nested_value(part, [2])
                                if not raw_body:
                                    continue
                                
                                # Peek for markers before expensive parsing
                                if "gg-dl" not in raw_body and img_mode_status is not None and img_mode_status < 3:
                                    if debug_mode:
                                        logger.debug(f"Chunk {part_idx}: Skipped (no /gg-dl/ marker, status < 3)")
                                    continue
                                
                                part_json = json.loads(raw_body)
                                score, part_cand = score_chunk(part_json, candidate_index, input_urls)
                                
                                if debug_mode:
                                    logger.debug(f"Chunk {part_idx}: score={score}")
                                
                                # EARLY EXIT on status=3 with good score
                                if score > best_score:
                                    best_score = score
                                    best_candidate = part_cand
                                    best_chunk_idx = part_idx
                                    
                                    # Check if this is a final chunk (status=3) with images
                                    cand_container, _ = find_image_container(part_cand)
                                    cand_status_arr = get_nested_value(cand_container, [6]) if cand_container else None
                                    cand_status = cand_status_arr[0] if isinstance(cand_status_arr, list) and cand_status_arr else -1
                                    
                                    if cand_status == 3 and score >= 30:
                                        if debug_mode:
                                            logger.debug(f"EARLY EXIT: status=3 with score={score}")
                                        break
                                
                            except json.JSONDecodeError:
                                continue
                        
                        if debug_mode:
                            logger.debug(f"Selected chunk {best_chunk_idx} with score={best_score}")
                        
                        if best_candidate is not None:
                            img_cand_container, _ = find_image_container(best_candidate)
                            
                            if img_cand_container:
                                # Update text if available
                                if finished_text := get_nested_value(best_candidate, [1, 0]):
                                    # Flexible pattern: handles http/https and potential subdomain variations
                                    processed_text = re.sub(
                                        r"https?://(?:\w+\.)?googleusercontent\.com/image_generation_content/\d+",
                                        "",
                                        finished_text,
                                    ).rstrip()
                                    if processed_text and processed_text.strip():
                                        text = processed_text
                                
                                # PRIORITY-BASED EXTRACTION
                                # Priority 1: Primary path [7] - JPEG (no watermark) first
                                # Priority 2: Primary path [7] - PNG (watermark)
                                # Priority 3: Fallback path [6]
                                
                                blobs = find_image_blobs(img_cand_container, input_urls=input_urls)
                                
                                if debug_mode:
                                    logger.debug(f"Found {len(blobs)} image blobs in selected chunk")
                                
                                # Sort blobs: no watermark first (position [6]), then watermarked (position [3])
                                no_watermark_blobs = [b for b in blobs if not b["is_watermarked"]]
                                watermarked_blobs = [b for b in blobs if b["is_watermarked"]]
                                
                                # Add non-watermarked images first (position [6])
                                for blob in no_watermark_blobs:
                                    if not any(img.url == blob["url"] for img in generated_images):
                                        if debug_mode:
                                            logger.debug(f"Adding image (no watermark, pos={blob.get('parent_index', '?')}): {blob['filename']}")
                                        generated_images.append(
                                            GeneratedImage(
                                                url=blob["url"],
                                                title=f"[NO_WATERMARK] {blob['filename']}" if blob['filename'] else "[NO_WATERMARK] Image",
                                                alt="",
                                                proxy=self.proxy,
                                                cookies=self.cookies,
                                            )
                                        )
                                
                                # Then add watermarked images (position [3])
                                for blob in watermarked_blobs:
                                    if not any(img.url == blob["url"] for img in generated_images):
                                        if debug_mode:
                                            logger.debug(f"Adding image (watermark, pos={blob.get('parent_index', '?')}): {blob['filename']}")
                                        generated_images.append(
                                            GeneratedImage(
                                                url=blob["url"],
                                                title=f"[WATERMARK] {blob['filename']}" if blob['filename'] else "[WATERMARK] Image",
                                                alt="",
                                                proxy=self.proxy,
                                                cookies=self.cookies,
                                            )
                                        )
                                
                                if debug_mode:
                                    logger.debug(f"Total extracted images: {len(generated_images)}")
                        
                        else:
                            if debug_mode:
                                logger.debug("Image mode detected but no suitable chunk found")
                    
                    output_candidates.append(
                        Candidate(
                            rcid=rcid,
                            text=text,
                            thoughts=thoughts,
                            web_images=web_images,
                            generated_images=generated_images,
                        )
                    )

                if not output_candidates:
                    raise GeminiError(
                        "Failed to generate contents. No output data found in response."
                    )

                output = ModelOutput(
                    metadata=get_nested_value(body, [1], []),
                    candidates=output_candidates,
                )
            except (TypeError, IndexError) as e:
                logger.debug(
                    f"{type(e).__name__}: {e}; Invalid response structure: {response.text}"
                )
                raise APIError(
                    "Failed to parse response body. Data structure is invalid."
                )

            if isinstance(chat, ChatSession):
                chat.last_output = output

            return output

    def start_chat(self, **kwargs) -> "ChatSession":
        """
        Returns a `ChatSession` object attached to this client.

        Parameters
        ----------
        kwargs: `dict`, optional
            Additional arguments which will be passed to the chat session.
            Refer to `gemini_webapi.ChatSession` for more information.

        Returns
        -------
        :class:`ChatSession`
            Empty chat session object for retrieving conversation history.
        """

        return ChatSession(geminiclient=self, **kwargs)

    async def _batch_execute(self, payloads: list[RPCData], **kwargs) -> Response:
        """
        Execute a batch of requests to Gemini API.

        Parameters
        ----------
        payloads: `list[GRPC]`
            List of `gemini_webapi.types.GRPC` objects to be executed.
        kwargs: `dict`, optional
            Additional arguments which will be passed to the post request.
            Refer to `httpx.AsyncClient.request` for more information.

        Returns
        -------
        :class:`httpx.Response`
            Response object containing the result of the batch execution.
        """

        try:
            response = await self.client.post(
                Endpoint.BATCH_EXEC,
                data={
                    "at": self.access_token,
                    "f.req": json.dumps(
                        [[payload.serialize() for payload in payloads]]
                    ).decode(),
                },
                **kwargs,
            )
        except ReadTimeout:
            raise TimeoutError(
                "Batch execute request timed out, please try again. If the problem persists, "
                "consider setting a higher `timeout` value when initializing GeminiClient."
            )

        # ? Seems like batch execution will immediately invalidate the current access token,
        # ? causing the next request to fail with 401 Unauthorized.
        if response.status_code != 200:
            await self.close()
            raise APIError(
                f"Batch execution failed with status code {response.status_code}"
            )

        return response


class ChatSession:
    """
    Chat data to retrieve conversation history. Only if all 3 ids are provided will the conversation history be retrieved.

    Parameters
    ----------
    geminiclient: `GeminiClient`
        Async httpx client interface for gemini.google.com.
    metadata: `list[str]`, optional
        List of chat metadata `[cid, rid, rcid]`, can be shorter than 3 elements, like `[cid, rid]` or `[cid]` only.
    cid: `str`, optional
        Chat id, if provided together with metadata, will override the first value in it.
    rid: `str`, optional
        Reply id, if provided together with metadata, will override the second value in it.
    rcid: `str`, optional
        Reply candidate id, if provided together with metadata, will override the third value in it.
    model: `Model | str | dict`, optional
        Specify the model to use for generation.
        Pass either a `gemini_webapi.constants.Model` enum or a model name string to use predefined models.
        Pass a dictionary to use custom model header strings ("model_name" and "model_header" keys must be provided).
    gem: `Gem | str`, optional
        Specify a gem to use as system prompt for the chat session.
        Pass either a `gemini_webapi.types.Gem` object or a gem id string.
    """

    __slots__ = [
        "__metadata",
        "geminiclient",
        "last_output",
        "model",
        "gem",
    ]

    def __init__(
        self,
        geminiclient: GeminiClient,
        metadata: list[str | None] | None = None,
        cid: str | None = None,  # chat id
        rid: str | None = None,  # reply id
        rcid: str | None = None,  # reply candidate id
        model: Model | str | dict = Model.UNSPECIFIED,
        gem: Gem | str | None = None,
    ):
        self.__metadata: list[str | None] = [None, None, None]
        self.geminiclient: GeminiClient = geminiclient
        self.last_output: ModelOutput | None = None
        self.model: Model | str | dict = model
        self.gem: Gem | str | None = gem

        if metadata:
            self.metadata = metadata
        if cid:
            self.cid = cid
        if rid:
            self.rid = rid
        if rcid:
            self.rcid = rcid

    def __str__(self):
        return f"ChatSession(cid='{self.cid}', rid='{self.rid}', rcid='{self.rcid}')"

    __repr__ = __str__

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        # update conversation history when last output is updated
        if name == "last_output" and isinstance(value, ModelOutput):
            self.metadata = value.metadata
            self.rcid = value.rcid

    async def send_message(
        self,
        prompt: str,
        files: list[str | Path] | None = None,
        **kwargs,
    ) -> ModelOutput:
        """
        Generates contents with prompt.
        Use as a shortcut for `GeminiClient.generate_content(prompt, image, self)`.

        Parameters
        ----------
        prompt: `str`
            Prompt provided by user.
        files: `list[str | Path]`, optional
            List of file paths to be attached.
        kwargs: `dict`, optional
            Additional arguments which will be passed to the post request.
            Refer to `httpx.AsyncClient.request` for more information.

        Returns
        -------
        :class:`ModelOutput`
            Output data from gemini.google.com, use `ModelOutput.text` to get the default text reply, `ModelOutput.images` to get a list
            of images in the default reply, `ModelOutput.candidates` to get a list of all answer candidates in the output.

        Raises
        ------
        `AssertionError`
            If prompt is empty.
        `gemini_webapi.TimeoutError`
            If request timed out.
        `gemini_webapi.GeminiError`
            If no reply candidate found in response.
        `gemini_webapi.APIError`
            - If request failed with status code other than 200.
            - If response structure is invalid and failed to parse.
        """

        return await self.geminiclient.generate_content(
            prompt=prompt,
            files=files,
            model=self.model,
            gem=self.gem,
            chat=self,
            **kwargs,
        )

    def choose_candidate(self, index: int) -> ModelOutput:
        """
        Choose a candidate from the last `ModelOutput` to control the ongoing conversation flow.

        Parameters
        ----------
        index: `int`
            Index of the candidate to choose, starting from 0.

        Returns
        -------
        :class:`ModelOutput`
            Output data of the chosen candidate.

        Raises
        ------
        `ValueError`
            If no previous output data found in this chat session, or if index exceeds the number of candidates in last model output.
        """

        if not self.last_output:
            raise ValueError("No previous output data found in this chat session.")

        if index >= len(self.last_output.candidates):
            raise ValueError(
                f"Index {index} exceeds the number of candidates in last model output."
            )

        self.last_output.chosen = index
        self.rcid = self.last_output.rcid
        return self.last_output

    @property
    def metadata(self):
        return self.__metadata

    @metadata.setter
    def metadata(self, value: list[str]):
        if len(value) > 3:
            raise ValueError("metadata cannot exceed 3 elements")
        self.__metadata[: len(value)] = value

    @property
    def cid(self):
        return self.__metadata[0]

    @cid.setter
    def cid(self, value: str):
        self.__metadata[0] = value

    @property
    def rid(self):
        return self.__metadata[1]

    @rid.setter
    def rid(self, value: str):
        self.__metadata[1] = value

    @property
    def rcid(self):
        return self.__metadata[2]

    @rcid.setter
    def rcid(self, value: str):
        self.__metadata[2] = value
