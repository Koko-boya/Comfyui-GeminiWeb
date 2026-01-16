"""
ComfyUI-Gemini: Unified Gemini Web node for image generation, editing, and chat.

Single node that handles authentication and all operations.
"""

import os
from .utils import tensor_to_pil, pil_to_tensor, bytes_to_tensor, save_temp_image, run_async
from .gemini_webapi.utils import logger

# Available Gemini models
GEMINI_MODELS = [
    "unspecified",
    "gemini-3-flash",
    "gemini-3-thinking",
    "gemini-3-pro",
]

# Available modes
GEMINI_MODES = [
    "text_to_image",
    "image_to_image", 
    "chat",
]

# Cache for client instances (to avoid re-initializing on every run)
_client_cache = {}


class GeminiWeb:
    """
    Unified Gemini Web node for text-to-image, image-to-image, and chat.
    
    Handles authentication and all operations in a single node.
    
    Modes:
    - text_to_image: Generate images from text prompts
    - image_to_image: Edit/transform images using text prompts  
    - chat: Chat with Gemini (optional image input for vision)
    """
    
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "mode": (GEMINI_MODES, {
                    "default": "text_to_image",
                    "tooltip": "Operation mode"
                }),
                "prompt": ("STRING", {
                    "default": "Generate a beautiful landscape",
                    "multiline": True,
                    "tooltip": "Text prompt"
                }),
                "auth_method": (["auto_cookies", "cookie_file", "manual"], {
                    "default": "auto_cookies",
                    "tooltip": "Cookie source: auto_cookies (from browser), cookie_file (from gemini_cookies.txt), manual (paste values)"
                }),
            },
            "optional": {
                "image_1": ("IMAGE", {
                    "tooltip": "Input image 1 (required for image_to_image)"
                }),
                "image_2": ("IMAGE", {
                    "tooltip": "Input image 2 (optional reference)"
                }),
                "image_3": ("IMAGE", {
                    "tooltip": "Input image 3 (optional reference)"
                }),
                "image_4": ("IMAGE", {
                    "tooltip": "Input image 4 (optional reference)"
                }),
                "image_5": ("IMAGE", {
                    "tooltip": "Input image 5 (optional reference)"
                }),
                "model": (GEMINI_MODELS, {
                    "default": "gemini-3-flash",
                    "tooltip": "Gemini model"
                }),
                "timeout": ("INT", {
                    "default": 120,
                    "min": 30,
                    "max": 600,
                    "tooltip": "API timeout in seconds"
                }),
                "image_filter": (["all", "no_watermark", "watermarked"], {
                    "default": "all",
                    "tooltip": "Filter: all=both, no_watermark=JPEG only, watermarked=PNG only"
                }),
                "cookie_1PSID": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "__Secure-1PSID (manual)"
                }),
                "cookie_1PSIDTS": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "__Secure-1PSIDTS (optional)"
                }),
                "debug_mode": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "Save request/response to debug files"
                }),
            }
        }
    
    RETURN_TYPES = ("IMAGE", "STRING", "STRING")
    RETURN_NAMES = ("image", "response_text", "thinking")
    FUNCTION = "execute"
    CATEGORY = "Gemini"
    DESCRIPTION = "Gemini Web: text-to-image, image-to-image, or chat"
    
    def _get_client(self, auth_method, cookie_1PSID="", cookie_1PSIDTS=""):
        """Get or create a cached Gemini client."""
        from .gemini_webapi import GeminiClient
        
        # Handle cookie_file method - read cookies from file
        if auth_method == "cookie_file":
            cookie_1PSID, cookie_1PSIDTS = self._load_cookies_from_file()
            auth_method = "manual"  # Treat as manual after loading
        
        # Create cache key using hash for uniqueness
        if auth_method == "auto_cookies":
            cache_key = "auto"
        else:
            import hashlib
            cookie_hash = hashlib.md5(cookie_1PSID.encode()).hexdigest()[:16] if cookie_1PSID else "empty"
            cache_key = f"manual:{cookie_hash}"
        
        # Return cached client if valid
        if cache_key in _client_cache:
            client = _client_cache[cache_key]
            if client._running:
                return client
        
        # Create new client
        async def init_client():
            if auth_method == "auto_cookies":
                client = GeminiClient()
            else:
                if not cookie_1PSID:
                    raise ValueError("cookie_1PSID required for manual auth")
                client = GeminiClient(
                    cookie_1PSID,
                    cookie_1PSIDTS if cookie_1PSIDTS else None,
                )
            
            await client.init(timeout=60, auto_close=False, auto_refresh=True)
            return client
        
        try:
            client = run_async(init_client())
        except Exception as e:
            error_msg = str(e)
            # Check for common auth errors and provide user-friendly messages
            if "No valid cookies available" in error_msg or "Cookie Authentication Failed" in error_msg or "AuthError" in type(e).__name__:
                # Pass through the detailed error message from the exception
                raise ValueError(error_msg) from None
            else:
                raise
        _client_cache[cache_key] = client
        return client
    
    def _load_cookies_from_file(self):
        """Load cookies from gemini_cookies.txt file.
        
        File format:
        Line 1: __Secure-1PSID value
        Line 2: __Secure-1PSIDTS value (optional)
        
        Or key=value format:
        __Secure-1PSID=value
        __Secure-1PSIDTS=value
        """
        cookie_file = os.path.join(os.path.dirname(__file__), "gemini_cookies.txt")
        
        if not os.path.exists(cookie_file):
            raise ValueError(
                f"Cookie file not found: {cookie_file}\n"
                f"Create gemini_cookies.txt with your cookie values:\n"
                f"Line 1: __Secure-1PSID value\n"
                f"Line 2: __Secure-1PSIDTS value (optional)"
            )
        
        with open(cookie_file, 'r', encoding='utf-8') as f:
            lines = [line.strip() for line in f.readlines() if line.strip() and not line.startswith('#')]
        
        cookie_1PSID = ""
        cookie_1PSIDTS = ""
        
        for line in lines:
            if '=' in line:
                # Key=value format
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip()
                if key == "__Secure-1PSID" or key == "PSID":
                    cookie_1PSID = value
                elif key == "__Secure-1PSIDTS" or key == "PSIDTS":
                    cookie_1PSIDTS = value
            elif not cookie_1PSID:
                # First non-key=value line is PSID
                cookie_1PSID = line
            elif not cookie_1PSIDTS:
                # Second non-key=value line is PSIDTS
                cookie_1PSIDTS = line
        
        if not cookie_1PSID:
            raise ValueError(
                f"No __Secure-1PSID found in {cookie_file}\n"
                f"Make sure the file contains your cookie values."
            )
        
        logger.info(f"Loaded cookies from {cookie_file}")
        return cookie_1PSID, cookie_1PSIDTS
    
    def execute(self, mode, prompt, auth_method, 
                image_1=None, image_2=None, image_3=None, image_4=None, image_5=None,
                model="gemini-3-flash", timeout=120, image_filter="all", 
                cookie_1PSID="", cookie_1PSIDTS="", debug_mode=False):
        import torch
        
        # Collect all provided images into a list
        images = [img for img in [image_1, image_2, image_3, image_4, image_5] if img is not None]
        
        # Get or create client
        client = self._get_client(auth_method, cookie_1PSID, cookie_1PSIDTS)
        
        if mode == "text_to_image":
            return self._text_to_image(client, prompt, model, timeout, image_filter, debug_mode)
        elif mode == "image_to_image":
            if not images:
                raise ValueError("image_to_image mode requires at least one input image")
            return self._image_to_image(client, images, prompt, model, timeout, image_filter, debug_mode)
        elif mode == "chat":
            return self._chat(client, prompt, images, model, timeout, debug_mode)
        else:
            raise ValueError(f"Unknown mode: {mode}")
    
    def _filter_images(self, images, image_filter):
        """Filter images based on watermark preference.
        
        Images are tagged by client.py based on their position in the response:
        - Path index 3 = [WATERMARK] (first image)
        - Path index 6 = [NO_WATERMARK] (second image)
        """
        if image_filter == "all":
            return images
        
        filtered = []
        for img in images:
            title = img.title.upper()
            
            if image_filter == "watermarked" and "[WATERMARK]" in title:
                filtered.append(img)
            elif image_filter == "no_watermark" and "[NO_WATERMARK]" in title:
                filtered.append(img)
        
        return filtered if filtered else images  # Fallback to all if filter returns nothing
    
    def _text_to_image(self, client, prompt, model, timeout=120, image_filter="all", debug_mode=False):
        """Generate images from text prompts."""
        import torch
        
        async def do_generate():
            response = await client.generate_content(
                prompt,
                model=model,
                image_mode=True,
                timeout=timeout,
                debug_mode=debug_mode
            )
            return response
        
        response = run_async(do_generate())
        response_text = response.text if response.text else ""
        thinking = self._get_thinking(response)
        
        if not response.images:
            logger.info(f"No images generated. Response: {response_text[:200] if response_text else 'No text'}")
            placeholder = torch.zeros((1, 512, 512, 3), dtype=torch.float32)
            return (placeholder, response_text, thinking)
        
        # Apply image filter (watermark/no_watermark)
        filtered_images = self._filter_images(response.images, image_filter)
        logger.info(f"Total: {len(response.images)} images, after filter '{image_filter}': {len(filtered_images)} images")
        
        image_tensors = self._download_all_images(filtered_images)
        return (image_tensors, response_text, thinking)
    
    def _image_to_image(self, client, images, prompt, model, timeout=120, image_filter="all", debug_mode=False):
        """Edit images using text prompts. Accepts list of image tensors."""
        import torch
        
        temp_paths = []
        
        try:
            # Save all input images to temp files
            logger.info(f"Processing {len(images)} input image(s) with model='{model}'")
            for img_tensor in images:
                pil_image = tensor_to_pil(img_tensor)
                temp_path = save_temp_image(pil_image)
                temp_paths.append(temp_path)
            
            async def do_edit():
                response = await client.generate_content(
                    prompt,
                    files=temp_paths,
                    model=model,
                    image_mode=True,
                    timeout=timeout,
                    debug_mode=debug_mode
                )
                return response
            
            response = run_async(do_edit())
        finally:
            # Clean up all temp files
            for temp_path in temp_paths:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
        
        response_text = response.text if response.text else ""
        thinking = self._get_thinking(response)
        
        logger.debug(f"Extracted response_text: '{response_text[:100] if response_text else 'EMPTY'}...'")
        
        if not response.images:
            logger.info(f"No images in response. Response: {response_text[:200] if response_text else 'No text'}")
            # Return first input image as fallback
            return (images[0], response_text, thinking)
        
        # Apply image filter (watermark/no_watermark)
        filtered_images = self._filter_images(response.images, image_filter)
        logger.info(f"Total: {len(response.images)} images, after filter '{image_filter}': {len(filtered_images)} images")
        
        image_tensors = self._download_all_images(filtered_images)
        return (image_tensors, response_text, thinking)
    
    def _chat(self, client, prompt, images, model, timeout=120, debug_mode=False):
        """Chat with Gemini, optionally with multiple image inputs."""
        import torch
        
        temp_paths = []
        
        try:
            # Save all input images to temp files
            if images:
                logger.info(f"Chat with {len(images)} image(s)")
                for img_tensor in images:
                    pil_image = tensor_to_pil(img_tensor)
                    temp_path = save_temp_image(pil_image)
                    temp_paths.append(temp_path)
            
            async def do_chat():
                response = await client.generate_content(
                    prompt,
                    files=temp_paths if temp_paths else None,
                    model=model,
                    timeout=timeout,
                    debug_mode=debug_mode
                )
                return response
            
            response = run_async(do_chat())
        finally:
            # Clean up all temp files
            for temp_path in temp_paths:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
        
        response_text = response.text if response.text else ""
        thinking = self._get_thinking(response)
        
        # Check if there are images in response
        if response.images:
            logger.info(f"Generated {len(response.images)} image(s)")
            image_tensors = self._download_all_images(response.images)
            return (image_tensors, response_text, thinking)
        
        # No image output
        if images:
            return (images[0], response_text, thinking)
        else:
            placeholder = torch.zeros((1, 512, 512, 3), dtype=torch.float32)
            return (placeholder, response_text, thinking)
    
    def _get_thinking(self, response):
        """Extract thinking/thoughts from the first candidate."""
        if response.candidates and len(response.candidates) > 0:
            thoughts = response.candidates[0].thoughts
            return thoughts if thoughts else ""
        return ""
    
    def _download_all_images(self, image_list):
        """Download ALL images and return as a batched tensor."""
        import torch
        import tempfile
        
        async def download_all():
            tensors = []
            for idx, image_obj in enumerate(image_list):
                fd, temp_path = tempfile.mkstemp(suffix=".png")
                os.close(fd)
                
                try:
                    await image_obj.save(path=os.path.dirname(temp_path), filename=os.path.basename(temp_path))
                    from PIL import Image as PILImage
                    pil_img = PILImage.open(temp_path)
                    tensor = pil_to_tensor(pil_img)
                    tensors.append(tensor)
                except Exception as e:
                    logger.warning(f"Failed to download image {idx + 1}: {e}")
                    # Continue with other images instead of failing completely
                finally:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
            
            # Stack all tensors into a batch
            if tensors:
                return torch.cat(tensors, dim=0)
            else:
                logger.warning("No images could be downloaded, returning placeholder")
                return torch.zeros((1, 512, 512, 3), dtype=torch.float32)
        
        return run_async(download_all())


# Node registration - single unified node
NODE_CLASS_MAPPINGS = {
    "GeminiWeb": GeminiWeb,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "GeminiWeb": "Gemini Web",
}
