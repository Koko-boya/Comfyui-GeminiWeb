"""
ComfyUI-Gemini: Utility functions for image tensor conversion and async handling.
"""

import numpy as np
from PIL import Image
import asyncio
import io
import tempfile
import os


def tensor_to_pil(image_tensor):
    """
    Convert ComfyUI IMAGE tensor to PIL Image.
    
    ComfyUI uses tensors with shape (batch, height, width, channels) 
    with values in range [0.0, 1.0] and RGB format.
    
    Args:
        image_tensor: torch.Tensor with shape (B, H, W, C)
    
    Returns:
        PIL.Image: First image from the batch
    """
    # Take first image from batch
    if len(image_tensor.shape) == 4:
        image_tensor = image_tensor[0]
    
    # Convert to numpy and scale to 0-255
    np_image = image_tensor.cpu().numpy()
    np_image = (np_image * 255).clip(0, 255).astype(np.uint8)
    
    return Image.fromarray(np_image, mode='RGB')


def pil_to_tensor(pil_image):
    """
    Convert PIL Image to ComfyUI IMAGE tensor.
    
    Args:
        pil_image: PIL.Image object
    
    Returns:
        torch.Tensor: Image tensor with shape (1, H, W, C)
    """
    import torch
    
    # Ensure RGB mode
    if pil_image.mode != 'RGB':
        pil_image = pil_image.convert('RGB')
    
    # Convert to numpy array
    np_image = np.array(pil_image).astype(np.float32) / 255.0
    
    # Add batch dimension: (H, W, C) -> (1, H, W, C)
    tensor = torch.from_numpy(np_image).unsqueeze(0)
    
    return tensor


def bytes_to_tensor(image_bytes):
    """
    Convert raw image bytes to ComfyUI IMAGE tensor.
    
    Args:
        image_bytes: bytes of an image file
    
    Returns:
        torch.Tensor: Image tensor with shape (1, H, W, C)
    """
    pil_image = Image.open(io.BytesIO(image_bytes))
    return pil_to_tensor(pil_image)


def save_temp_image(pil_image, suffix=".png"):
    """
    Save PIL image to a temporary file and return the path.
    
    Args:
        pil_image: PIL.Image object
        suffix: File extension (default: .png)
    
    Returns:
        str: Path to the temporary file
    """
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    pil_image.save(path)
    return path


import threading

# Thread-local storage for event loops
_loop_storage = threading.local()


def _get_or_create_event_loop():
    """
    Get or create an event loop for the current thread.
    This ensures we reuse the same loop and avoid "Event loop is closed" errors.
    """
    loop = getattr(_loop_storage, 'loop', None)
    
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _loop_storage.loop = loop
    
    return loop


def run_async(coro):
    """
    Run an async coroutine in a synchronous context.
    Handles the case where there may or may not be an existing event loop.
    Uses a persistent thread-local event loop to avoid "Event loop is closed" errors.
    
    Args:
        coro: Async coroutine to run
    
    Returns:
        The result of the coroutine
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    
    if loop and loop.is_running():
        # We're in an async context, create a new thread to run the coroutine
        import concurrent.futures
        
        def run_in_new_loop():
            new_loop = _get_or_create_event_loop()
            return new_loop.run_until_complete(coro)
        
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(run_in_new_loop)
            return future.result()
    else:
        # No running loop, use our persistent event loop
        loop = _get_or_create_event_loop()
        return loop.run_until_complete(coro)
