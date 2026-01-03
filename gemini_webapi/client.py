import asyncio
import re
from asyncio import Task
from pathlib import Path
from typing import Any, Optional

import orjson as json
from httpx import AsyncClient, ReadTimeout, Response

from .components import GemMixin
from .constants import Endpoint, ErrorCode, GRPC, Headers, Model
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
            
            # For image mode, build header with correct extended format
            # Image mode REQUIRES a valid model ID - default to gemini-2.5-flash if unspecified
            model_header_str = model.model_header.get("x-goog-ext-525001261-jspb", "")
            if '"' in model_header_str:
                # Extract model ID from existing header
                model_id = model_header_str.split('"')[1]
            else:
                # Default to gemini-2.5-flash model ID for image mode
                model_id = "9ec249fc9ad08861"  # G_2_5_FLASH model ID
            
            final_headers["x-goog-ext-525001261-jspb"] = f'[1,null,null,null,"{model_id}",null,null,0,[4],null,null,2]'
            final_headers["x-goog-ext-525005358-jspb"] = f'["{session_uuid}",1]'
            # Note: x-goog-ext-73010989-jspb is now included in base GEMINI headers
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
                # Image mode: Build a 67-element array matching HAR structure
                import time
                current_time = int(time.time())
                timestamp_ns = int((time.time() % 1) * 1000000000)
                
                # Define required values at specific positions for image generation
                image_mode_values = {
                    0: prompt_data,
                    1: ["en"],
                    2: chat_metadata,
                    7: 1,               # Enable image mode
                    10: 1,              # Image mode flag
                    17: [[0]],          # Image mode indicator
                    27: 1,              # Required flag
                    30: [4],            # Image format indicator
                    41: [1],            # Required flag
                    49: 14,             # Image generation type
                    59: session_uuid,   # Session UUID
                    66: [current_time, timestamp_ns],  # Timestamp
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
                    with open(os.path.join(debug_dir, "debug_response.txt"), "w", encoding="utf-8") as f:
                        f.write(response.text)
                    print(f"[Gemini Debug] Saved response to {debug_dir}/debug_response.txt")
                except Exception as e:
                    logger.debug(f"Failed to save response: {e}")
            
            response_json: list[Any] = []

            body: list[Any] = []
            body_index = 0

            try:
                response_json = extract_json_from_response(response.text)

                for part_index, part in enumerate(response_json):
                    try:
                        part_body = get_nested_value(part, [2])
                        if not part_body:
                            continue

                        part_json = json.loads(part_body)
                        if get_nested_value(part_json, [4]):
                            body_index, body = part_index, part_json
                            break
                    except json.JSONDecodeError:
                        continue

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

                    # Generated images
                    generated_images = []
                    
                    # Check if this is an image generation response
                    # candidate[12][6] indicates image mode status:
                    #   [0] = still generating, [2] or [3] = complete with images
                    # We need to find the LAST chunk with complete images, not the first
                    candidate_12 = get_nested_value(candidate, [12])
                    is_image_mode = (
                        isinstance(candidate_12, list) and 
                        len(candidate_12) > 6 and 
                        isinstance(candidate_12[6], list) and
                        len(candidate_12[6]) > 0
                    )
                    
                    if is_image_mode:
                        img_mode_status = candidate_12[6][0] if candidate_12[6] else None
                        if debug_mode:
                            logger.debug(f"Image mode detected (candidate[12][6] = {candidate_12[6]}), status={img_mode_status}")
                        
                        # Search through ALL response parts and keep the LAST one with complete images
                        # Complete = [12][6][0] >= 2 (not 0 which means still generating)
                        img_body = None
                        best_status = -1
                        
                        for img_part_index, part in enumerate(response_json):
                            try:
                                img_part_body = get_nested_value(part, [2])
                                if not img_part_body:
                                    continue

                                img_part_json = json.loads(img_part_body)
                                
                                # Check the status of this part's [12][6] 
                                part_candidate = get_nested_value(img_part_json, [4, candidate_index])
                                part_status_arr = get_nested_value(part_candidate, [12, 6])
                                part_status = part_status_arr[0] if isinstance(part_status_arr, list) and part_status_arr else -1
                                
                                # We want the part with highest status (2 or 3 = complete)
                                # Also check if it has actual image data
                                if isinstance(part_status, int) and part_status >= best_status:
                                    # Check for image data at [12][6][1] (image-to-image) or [12][7][0] (text-to-image)
                                    has_img_data = (
                                        get_nested_value(part_candidate, [12, 6, 1]) or
                                        get_nested_value(part_candidate, [12, 7, 0])
                                    )
                                    if has_img_data or part_status > best_status:
                                        best_status = part_status
                                        img_body = img_part_json
                                        if debug_mode:
                                            logger.debug(f"Found better response part {img_part_index} with status={part_status}")
                            except json.JSONDecodeError:
                                continue
                        
                        if debug_mode:
                            logger.debug(f"Selected response part with status={best_status}")

                        if img_body:
                            img_candidate = get_nested_value(
                                img_body, [4, candidate_index], []
                            )

                            if finished_text := get_nested_value(
                                img_candidate, [1, 0]
                            ):  # Only overwrite if new text is returned after image generation
                                text = re.sub(
                                    r"http://googleusercontent\.com/image_generation_content/\d+",
                                    "",
                                    finished_text,
                                ).rstrip()

                            for img_index, gen_img_data in enumerate(
                                get_nested_value(img_candidate, [12, 7, 0], [])
                            ):
                                # Debug: Log the structure of gen_img_data to understand image locations
                                if debug_mode:
                                    logger.debug(f"gen_img_data[{img_index}] type: {type(gen_img_data)}, len: {len(gen_img_data) if isinstance(gen_img_data, list) else 'N/A'}")
                                    if isinstance(gen_img_data, list):
                                        for i, item in enumerate(gen_img_data[:5]):  # Log first 5 elements
                                            if item is not None:
                                                logger.debug(f"  gen_img_data[{img_index}][{i}] = {str(item)[:150]}")
                                
                                # Images can be at multiple paths within each gen_img_data:
                                # - First image: [0][3][3] → gen_img_data[0][3] is first image data
                                # - Second image: [0][6][3] → gen_img_data[0][6] is second image data (at index 6!)
                                # The structure is: [[null,null,null,[img1],null,null,[img2],...],...]
                                
                                image_paths = [
                                    [0, 3, 3],    # First image: gen_img_data[0][3][3]
                                    [0, 6, 3],    # Second image: gen_img_data[0][6][3] (index 6!)
                                    [0, 0, 3, 3], # Alternative nested path
                                    [0, 0, 6, 3], # Alternative for second image
                                ]
                                

                                for path in image_paths:
                                    url = get_nested_value(gen_img_data, path)
                                    if debug_mode:
                                        logger.debug(f"  Path {path} -> {str(url)[:100] if url else 'None'}")
                                    if url and isinstance(url, str) and url.startswith("http"):
                                        # Check if we already added this URL (avoid duplicates)
                                        if any(img.url == url for img in generated_images):
                                            if debug_mode:
                                                logger.debug(f"    Skipping duplicate URL")
                                            continue
                                        
                                        # Get filename from the same path but at index 2 instead of 3
                                        # Structure: [None, 1, 'filename.png', 'url', ...]
                                        filename_path = path[:-1] + [2]  # Replace last index (3) with 2
                                        filename = get_nested_value(gen_img_data, filename_path, "")
                                        
                                        # Determine watermark status by path index:
                                        # - Index 3 paths = watermarked (first image)
                                        # - Index 6 paths = no watermark (second image)
                                        path_index = path[1] if len(path) > 1 else path[-2] if len(path) > 2 else 3
                                        is_watermarked = path_index == 3 or path_index == 0  # 0, 3 = watermark; 6 = no watermark
                                        watermark_tag = "[WATERMARK]" if is_watermarked else "[NO_WATERMARK]"
                                        
                                        if debug_mode:
                                            logger.debug(f"    Filename: {filename}, Path index: {path_index}, Watermark: {is_watermarked}")
                                        
                                        # Title includes watermark tag for filtering
                                        img_num = len(generated_images) + 1
                                        title = f"{watermark_tag} {filename}" if filename else f"{watermark_tag} Image {img_num}"

                                        alt_list = (
                                            get_nested_value(gen_img_data, [3, 5], []) or
                                            get_nested_value(gen_img_data, [0, 3, 5], [])
                                        )
                                        alt = (
                                            get_nested_value(alt_list, [img_index])
                                            or get_nested_value(alt_list, [0])
                                            or ""
                                        )

                                        generated_images.append(
                                            GeneratedImage(
                                                url=url,
                                                title=title,
                                                alt=alt,
                                                proxy=self.proxy,
                                                cookies=self.cookies,
                                            )
                                        )

                                
                            if debug_mode:
                                logger.debug(f"Extracted {len(generated_images)} generated images from standard path")
                            
                            # Fallback: If no images found via standard paths, search in candidate[12][6]
                            # Image-to-image responses have images at [12][6][1][0][0][0][3] (PNG) and [12][6][1][0][0][3] (JPEG)
                            if not generated_images and img_candidate:
                                if debug_mode:
                                    logger.debug("No images from standard path, searching image-to-image path in candidate[12][6]...")
                                
                                # Image-to-image structure: candidate[12][6] = [2, [[[[null,null,null,PNG],null,null,JPEG]...]]]
                                img_edit_data = get_nested_value(img_candidate, [12, 6], None)
                                if img_edit_data and isinstance(img_edit_data, list) and len(img_edit_data) > 1:
                                    # Path to images: [12][6][1][0][0][0] contains the image array
                                    img_array = get_nested_value(img_edit_data, [1, 0, 0, 0], None)
                                    
                                    if img_array and isinstance(img_array, list):
                                        if debug_mode:
                                            logger.debug(f"Found image array at [12][6][1][0][0][0], len: {len(img_array)}")
                                        
                                        # PNG is at position 3: [null, null, null, [null,1,"file.png","url",...]]
                                        png_data = get_nested_value(img_array, [3], None)
                                        if png_data and isinstance(png_data, list) and len(png_data) > 3:
                                            url = png_data[3] if len(png_data) > 3 else None
                                            filename = png_data[2] if len(png_data) > 2 else ""
                                            if url and isinstance(url, str) and url.startswith("http") and "gg-dl" in url:
                                                if debug_mode:
                                                    logger.debug(f"Found PNG at [12][6][1][0][0][0][3]: {url[:80]}...")
                                                generated_images.append(
                                                    GeneratedImage(
                                                        url=url,
                                                        title=f"[WATERMARK] {filename}" if filename else "[WATERMARK] Image",
                                                        alt="",
                                                        proxy=self.proxy,
                                                        cookies=self.cookies,
                                                    )
                                                )
                                        
                                        # JPEG is at parent level position 3: get_nested_value([12][6][1][0][0], [3])
                                        jpeg_container = get_nested_value(img_edit_data, [1, 0, 0, 3], None)
                                        if jpeg_container and isinstance(jpeg_container, list) and len(jpeg_container) > 3:
                                            url = jpeg_container[3] if len(jpeg_container) > 3 else None
                                            filename = jpeg_container[2] if len(jpeg_container) > 2 else ""
                                            if url and isinstance(url, str) and url.startswith("http") and "gg-dl" in url:
                                                if debug_mode:
                                                    logger.debug(f"Found JPEG at [12][6][1][0][0][3]: {url[:80]}...")
                                                generated_images.append(
                                                    GeneratedImage(
                                                        url=url,
                                                        title=f"[NO_WATERMARK] {filename}" if filename else "[NO_WATERMARK] Image",
                                                        alt="",
                                                        proxy=self.proxy,
                                                        cookies=self.cookies,
                                                    )
                                                )
                                
                                if debug_mode:
                                    logger.debug(f"Total extracted after image-to-image path: {len(generated_images)} images")
                        else:
                            if debug_mode:
                                logger.debug("Image mode detected but no image data found in any response part")

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
