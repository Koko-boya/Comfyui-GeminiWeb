"""
ComfyUI-Gemini

Custom nodes for Google Gemini image generation and editing in ComfyUI.
Uses Gemini web interface for free image generation.

Based on: https://github.com/HanaokaYuzu/Gemini-API (vendored)
"""

import subprocess
import sys

# Auto-install missing dependencies
def check_and_install():
    packages_to_install = {
        "httpx": "httpx",              # HTTP client
        "orjson": "orjson",            # Fast JSON parsing
        "browser_cookie3": "browser-cookie3",  # Auto-cookie extraction
        "loguru": "loguru",            # Logging
    }
    
    for import_name, pip_name in packages_to_install.items():
        try:
            __import__(import_name)
        except ImportError:
            print(f"[ComfyUI-Gemini] Installing {pip_name}...")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", "-U", pip_name])
                print(f"[ComfyUI-Gemini] ✓ Installed {pip_name}")
            except Exception as e:
                print(f"[ComfyUI-Gemini] ✗ Failed to install {pip_name}: {e}")
                print(f"[ComfyUI-Gemini] Please run manually: {sys.executable} -m pip install {pip_name}")

check_and_install()

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

__version__ = "1.1.0"
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
