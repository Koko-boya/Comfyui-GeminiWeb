"""
Unified Browser Cookie Extraction for Gemini Authentication.

This module provides a clean, unified interface for extracting browser cookies
with support for:
- v10/v11 encryption (older browsers, auto-decryption)  
- v20 App-Bound Encryption (Chrome 127+, Edge - requires admin)
- cookie_file method (manual, always works)

Usage:
    from gemini_webapi.utils.cookies import load_gemini_cookies
    
    # Auto-detect and load cookies
    cookies = load_gemini_cookies()
    
    # Or specify method
    cookies = load_gemini_cookies(method="cookie_file")
"""

import os
import json
import base64
import sqlite3
import shutil
import tempfile
import platform
from pathlib import Path
from typing import Optional

from .logger import logger


# ============================================================================
# Cookie File Method (Always Works)
# ============================================================================

def load_cookies_from_file(cookie_file_path: Optional[str] = None) -> dict[str, str]:
    """
    Load cookies from a text file.
    
    File format (one per line):
        __Secure-1PSID=value
        __Secure-1PSIDTS=value
    
    Or simple format:
        psid_value
        psidts_value
    """
    if cookie_file_path is None:
        # Look in common locations  
        locations = [
            Path.cwd() / "gemini_cookies.txt",
            Path(__file__).parent.parent.parent / "gemini_cookies.txt",
            Path(os.environ.get("USERPROFILE", "")) / "gemini_cookies.txt",
        ]
        for loc in locations:
            if loc.exists():
                cookie_file_path = str(loc)
                break
    
    if not cookie_file_path or not Path(cookie_file_path).exists():
        return {}
    
    cookies = {}
    try:
        with open(cookie_file_path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        
        for line in lines:
            if "=" in line:
                key, value = line.split("=", 1)
                cookies[key.strip()] = value.strip()
            elif len(lines) == 2 and "=" not in lines[0]:
                # Simple format: first line is PSID, second is PSIDTS
                cookies["__Secure-1PSID"] = lines[0]
                cookies["__Secure-1PSIDTS"] = lines[1]
                break
                
    except Exception as e:
        logger.debug(f"Error reading cookie file: {e}")
    
    return cookies


# ============================================================================
# v20 Decryption (Requires Admin + PythonForWindows)
# ============================================================================

def _try_v20_decryption(browser: str = "edge") -> dict[str, str]:
    """
    Attempt v20 decryption using PythonForWindows.
    Requires running as Administrator.
    """
    try:
        from .v20_decrypt import check_dependencies, extract_edge_cookies, get_chrome_v20_key
        
        deps = check_dependencies()
        if not deps["can_decrypt"]:
            if not deps["is_admin"]:
                logger.debug("v20 decryption requires Administrator privileges")
            return {}
        
        if browser == "edge":
            return extract_edge_cookies("google.com")
        else:
            # Chrome v20 decryption would go here
            return {}
            
    except ImportError:
        logger.debug("PythonForWindows not available for v20 decryption")
        return {}
    except Exception as e:
        logger.debug(f"v20 decryption failed: {e}")
        return {}


# ============================================================================
# v10/v11 Decryption (Standard DPAPI + AES-GCM)
# ============================================================================

def _get_v10_key(browser_name: str) -> Optional[bytes]:
    """Get the decryption key for v10/v11 cookies."""
    if platform.system() != "Windows":
        return None
    
    try:
        import win32crypt
    except ImportError:
        return None
    
    browser_paths = {
        "edge": Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "User Data",
        "chrome": Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data",
    }
    
    user_data_path = browser_paths.get(browser_name)
    if not user_data_path or not user_data_path.exists():
        return None
    
    local_state_path = user_data_path / "Local State"
    if not local_state_path.exists():
        return None
    
    try:
        with open(local_state_path, "r", encoding="utf-8") as f:
            local_state = json.load(f)
        
        encrypted_key = local_state.get("os_crypt", {}).get("encrypted_key", "")
        if not encrypted_key:
            return None
        
        encrypted_key = base64.b64decode(encrypted_key)[5:]  # Remove DPAPI prefix
        return win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]
        
    except Exception as e:
        logger.debug(f"Failed to get v10 key: {e}")
        return None


def _decrypt_cookie_value(encrypted_value: bytes, key: bytes) -> Optional[str]:
    """Decrypt a cookie value (v10/v11)."""
    if not encrypted_value or len(encrypted_value) < 15:
        return None
    
    # v20 detection - cannot decrypt
    if encrypted_value[:3] == b"v20":
        return None
    
    # v10/v11 decryption
    if encrypted_value[:3] in (b"v10", b"v11"):
        try:
            from Cryptodome.Cipher import AES
        except ImportError:
            try:
                from Crypto.Cipher import AES
            except ImportError:
                return None
        
        try:
            nonce = encrypted_value[3:15]
            ciphertext = encrypted_value[15:-16]
            tag = encrypted_value[-16:]
            
            cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
            return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")
        except Exception:
            return None
    
    return None


def _extract_cookies_v10(browser: str, domain_filter: str = "google.com") -> dict[str, str]:
    """Extract cookies using v10/v11 decryption."""
    key = _get_v10_key(browser)
    if not key:
        return {}
    
    browser_paths = {
        "edge": Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "User Data" / "Default" / "Network" / "Cookies",
        "chrome": Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data" / "Default" / "Network" / "Cookies",
    }
    
    cookie_path = browser_paths.get(browser)
    if not cookie_path or not cookie_path.exists():
        return {}
    
    cookies = {}
    temp_dir = None
    
    try:
        temp_dir = tempfile.mkdtemp()
        temp_cookie = os.path.join(temp_dir, "Cookies")
        shutil.copy2(cookie_path, temp_cookie)
        
        conn = sqlite3.connect(temp_cookie)
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT name, encrypted_value FROM cookies WHERE host_key LIKE ?",
            (f"%{domain_filter}%",)
        )
        
        for name, encrypted_value in cursor.fetchall():
            if encrypted_value[:3] == b"v20":
                continue  # Skip v20, cannot decrypt
            
            decrypted = _decrypt_cookie_value(encrypted_value, key)
            if decrypted:
                cookies[name] = decrypted
        
        conn.close()
        
    except Exception as e:
        logger.debug(f"v10 extraction failed: {e}")
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
    
    return cookies


# ============================================================================
# Main Interface
# ============================================================================

def load_gemini_cookies(
    method: str = "auto",
    cookie_file: Optional[str] = None,
    psid: Optional[str] = None,
    psidts: Optional[str] = None,
) -> dict[str, str]:
    """
    Load Gemini authentication cookies.
    
    Parameters
    ----------
    method : str
        - "auto" : Try all methods (v20 if admin, v10, cookie_file)
        - "cookie_file" : Read from gemini_cookies.txt
        - "manual" : Use provided psid/psidts values
        - "v20" : Force v20 decryption (requires admin)
        
    cookie_file : str, optional
        Path to cookie file (for cookie_file method)
        
    psid : str, optional
        Manual __Secure-1PSID value
        
    psidts : str, optional
        Manual __Secure-1PSIDTS value
        
    Returns
    -------
    dict[str, str]
        Cookie dict with at least __Secure-1PSID if successful
    """
    
    # Manual method
    if method == "manual" or psid:
        cookies = {}
        if psid:
            cookies["__Secure-1PSID"] = psid
        if psidts:
            cookies["__Secure-1PSIDTS"] = psidts
        return cookies
    
    # Cookie file method  
    if method == "cookie_file":
        cookies = load_cookies_from_file(cookie_file)
        if "__Secure-1PSID" in cookies:
            logger.debug("Loaded cookies from file")
            return cookies
        logger.warning("Cookie file not found or missing __Secure-1PSID")
        return {}
    
    # v20 method (explicit)
    if method == "v20":
        cookies = _try_v20_decryption("edge")
        if "__Secure-1PSID" in cookies:
            logger.info("Decrypted v20 cookies successfully")
            return cookies
        logger.warning("v20 decryption failed (need admin?)")
        return {}
    
    # Auto method - try everything
    if method == "auto":
        # 1. Try v10/v11 decryption first (no admin needed)
        for browser in ["edge", "chrome"]:
            cookies = _extract_cookies_v10(browser, "google.com")
            if "__Secure-1PSID" in cookies:
                logger.debug(f"Loaded v10/v11 cookies from {browser}")
                return cookies
        
        # 2. Try v20 if running as admin
        cookies = _try_v20_decryption("edge")
        if "__Secure-1PSID" in cookies:
            logger.info("Decrypted v20 cookies (admin mode)")
            return cookies
        
        # 3. Fall back to cookie file
        cookies = load_cookies_from_file(cookie_file)
        if "__Secure-1PSID" in cookies:
            logger.debug("Loaded cookies from file")
            return cookies
        
        # 4. Nothing worked
        logger.warning(
            "No cookies found. Modern browsers use v20 encryption. "
            "Use 'cookie_file' method or run as Administrator."
        )
        return {}
    
    return {}


def check_auth_status() -> dict:
    """
    Check authentication status and available methods.
    
    Returns a status dict useful for debugging.
    """
    import ctypes
    
    status = {
        "platform": platform.system(),
        "is_admin": False,
        "has_cookie_file": False,
        "v10_available": False,
        "v20_available": False,
        "recommended_method": "cookie_file",
    }
    
    # Check admin
    try:
        status["is_admin"] = ctypes.windll.shell32.IsUserAnAdmin() != 0
    except:
        pass
    
    # Check cookie file
    cookie_file = Path.cwd() / "gemini_cookies.txt"
    if cookie_file.exists():
        cookies = load_cookies_from_file(str(cookie_file))
        status["has_cookie_file"] = "__Secure-1PSID" in cookies
    
    # Check v10
    if platform.system() == "Windows":
        key = _get_v10_key("edge")
        status["v10_available"] = key is not None
    
    # Check v20
    try:
        from .v20_decrypt import check_dependencies
        deps = check_dependencies()
        status["v20_available"] = deps["can_decrypt"]
    except:
        pass
    
    # Recommend best method
    if status["v20_available"]:
        status["recommended_method"] = "auto"  # v20 will work
    elif status["has_cookie_file"]:
        status["recommended_method"] = "cookie_file"
    elif status["is_admin"]:
        status["recommended_method"] = "v20"
    else:
        status["recommended_method"] = "cookie_file"
    
    return status
