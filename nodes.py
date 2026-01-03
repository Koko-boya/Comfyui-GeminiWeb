"""
ComfyUI-Gemini: Unified Gemini Web node for image generation, editing, and chat.

Single node that handles authentication and all operations.
"""

import os
from .utils import tensor_to_pil, pil_to_tensor, bytes_to_tensor, save_temp_image, run_async

# Available Gemini models
GEMINI_MODELS = [
    "unspecified",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-3.0-pro",
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
                "auth_method": (["auto_cookies", "manual"], {
                    "default": "auto_cookies",
                    "tooltip": "Cookie source"
                }),
            },
            "optional": {
                "image": ("IMAGE", {
                    "tooltip": "Input image (image_to_image/chat)"
                }),
                "model": (GEMINI_MODELS, {
                    "default": "gemini-2.5-flash",
                    "tooltip": "Gemini model"
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
        
        # Create cache key
        if auth_method == "auto_cookies":
            cache_key = "auto"
        else:
            cache_key = f"manual:{cookie_1PSID[:20] if cookie_1PSID else 'empty'}"
        
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
        
        client = run_async(init_client())
        _client_cache[cache_key] = client
        return client
    
    def execute(self, mode, prompt, auth_method, image=None, model="gemini-2.5-flash", 
                cookie_1PSID="", cookie_1PSIDTS=""):
        import torch
        
        # Get or create client
        client = self._get_client(auth_method, cookie_1PSID, cookie_1PSIDTS)
        
        if mode == "text_to_image":
            return self._text_to_image(client, prompt, model)
        elif mode == "image_to_image":
            if image is None:
                raise ValueError("image_to_image mode requires an input image")
            return self._image_to_image(client, image, prompt, model)
        elif mode == "chat":
            return self._chat(client, prompt, image, model)
        else:
            raise ValueError(f"Unknown mode: {mode}")
    
    def _text_to_image(self, client, prompt, model):
        """Generate images from text prompts. Returns ALL images as a batch."""
        import torch
        
        async def do_generate():
            response = await client.generate_content(
                prompt,
                model=model if model != "unspecified" else None
            )
            return response
        
        response = run_async(do_generate())
        response_text = response.text if response.text else ""
        thinking = self._get_thinking(response)
        
        if not response.images:
            print("[Gemini] No images generated. Response:", response_text[:200] if response_text else "No text")
            placeholder = torch.zeros((1, 512, 512, 3), dtype=torch.float32)
            return (placeholder, response_text, thinking)
        
        print(f"[Gemini] Generated {len(response.images)} image(s)")
        image_tensors = self._download_all_images(response.images)
        return (image_tensors, response_text, thinking)
    
    def _image_to_image(self, client, image, prompt, model):
        """Edit images using text prompts. Returns ALL images as a batch."""
        import torch
        
        pil_image = tensor_to_pil(image)
        temp_path = save_temp_image(pil_image)
        
        try:
            async def do_edit():
                response = await client.generate_content(
                    prompt,
                    files=[temp_path],
                    model=model if model != "unspecified" else None
                )
                return response
            
            response = run_async(do_edit())
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        
        response_text = response.text if response.text else ""
        thinking = self._get_thinking(response)
        
        if not response.images:
            print("[Gemini] No images in response. Response:", response_text[:200] if response_text else "No text")
            return (image, response_text, thinking)
        
        print(f"[Gemini] Generated {len(response.images)} image(s)")
        image_tensors = self._download_all_images(response.images)
        return (image_tensors, response_text, thinking)
    
    def _chat(self, client, prompt, image, model):
        """Chat with Gemini, optionally with image input."""
        import torch
        
        temp_path = None
        files = None
        
        try:
            if image is not None:
                pil_image = tensor_to_pil(image)
                temp_path = save_temp_image(pil_image)
                files = [temp_path]
            
            async def do_chat():
                response = await client.generate_content(
                    prompt,
                    files=files,
                    model=model if model != "unspecified" else None
                )
                return response
            
            response = run_async(do_chat())
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
        
        response_text = response.text if response.text else ""
        thinking = self._get_thinking(response)
        
        # Check if there are images in response
        if response.images:
            print(f"[Gemini] Generated {len(response.images)} image(s)")
            image_tensors = self._download_all_images(response.images)
            return (image_tensors, response_text, thinking)
        
        # No image output
        if image is not None:
            return (image, response_text, thinking)
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
            for image_obj in image_list:
                fd, temp_path = tempfile.mkstemp(suffix=".png")
                os.close(fd)
                
                try:
                    await image_obj.save(path=os.path.dirname(temp_path), filename=os.path.basename(temp_path))
                    from PIL import Image as PILImage
                    pil_img = PILImage.open(temp_path)
                    tensor = pil_to_tensor(pil_img)
                    tensors.append(tensor)
                finally:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
            
            # Stack all tensors into a batch
            if tensors:
                return torch.cat(tensors, dim=0)
            else:
                return torch.zeros((1, 512, 512, 3), dtype=torch.float32)
        
        return run_async(download_all())


# Node registration - single unified node
NODE_CLASS_MAPPINGS = {
    "GeminiWeb": GeminiWeb,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "GeminiWeb": "Gemini Web",
}
