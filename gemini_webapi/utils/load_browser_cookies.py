"""
Custom browser cookie extraction for Windows.
Supports Edge, Chrome, Brave, Vivaldi, Chromium.

This implementation directly reads and decrypts browser cookies.

IMPORTANT: Chrome 127+ and Edge use v20 App-Bound Encryption which
CANNOT be decrypted by external scripts. If cookies are v20-encrypted,
users must use the 'cookie_file' or 'manual' authentication method.

v10/v11 cookies can still be decrypted using DPAPI + AES-GCM.
"""

import json
import base64
import sqlite3
import shutil
import tempfile
import os
import platform
from pathlib import Path
from http.cookiejar import CookieJar

from .logger import logger


def get_chromium_browser_paths() -> dict[str, Path]:
    """Get user data paths for all Chromium-based browsers on Windows."""
    if platform.system() != "Windows":
        return {}
    
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    app_data = os.environ.get("APPDATA", "")
    
    if not local_app_data:
        return {}
    
    paths = {
        "edge": Path(local_app_data) / "Microsoft" / "Edge" / "User Data",
        "chrome": Path(local_app_data) / "Google" / "Chrome" / "User Data",
        "brave": Path(local_app_data) / "BraveSoftware" / "Brave-Browser" / "User Data",
        "vivaldi": Path(local_app_data) / "Vivaldi" / "User Data",
        "chromium": Path(local_app_data) / "Chromium" / "User Data",
    }
    
    if app_data:
        paths["opera"] = Path(app_data) / "Opera Software" / "Opera Stable"
    
    return {name: path for name, path in paths.items() if path.exists()}


def get_chromium_profiles(browser_name: str) -> list[Path]:
    """
    Get all profile directories for a Chromium-based browser.
    Returns a list of paths to cookie files for all profiles.
    """
    browser_paths = get_chromium_browser_paths()
    user_data_path = browser_paths.get(browser_name)
    
    if not user_data_path:
        return []
    
    cookie_files = []
    
    # Check Default profile
    default_cookies = user_data_path / "Default" / "Network" / "Cookies"
    if default_cookies.exists():
        cookie_files.append(default_cookies)
    
    # Check numbered profiles (Profile 1, Profile 2, etc.)
    for profile_dir in user_data_path.iterdir():
        if profile_dir.is_dir() and profile_dir.name.startswith("Profile "):
            profile_cookies = profile_dir / "Network" / "Cookies"
            if profile_cookies.exists():
                cookie_files.append(profile_cookies)
    
    return cookie_files


def get_decryption_key(browser_name: str) -> bytes | None:
    """Get the cookie decryption key for a Chromium browser on Windows."""
    if platform.system() != "Windows":
        return None
    
    try:
        import win32crypt
    except ImportError:
        logger.warning("win32crypt not available. Install pywin32: pip install pywin32")
        return None
    
    browser_paths = get_chromium_browser_paths()
    user_data_path = browser_paths.get(browser_name)
    
    if not user_data_path:
        return None
    
    local_state_path = user_data_path / "Local State"
    if not local_state_path.exists():
        return None
    
    try:
        with open(local_state_path, 'r', encoding='utf-8') as f:
            local_state = json.load(f)
        
        encrypted_key_b64 = local_state.get('os_crypt', {}).get('encrypted_key', '')
        if not encrypted_key_b64:
            return None
        
        # Decode and remove DPAPI prefix (first 5 bytes)
        encrypted_key = base64.b64decode(encrypted_key_b64)[5:]
        
        # Decrypt using Windows DPAPI
        decrypted_key = win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]
        return decrypted_key
        
    except Exception as e:
        logger.debug(f"Failed to get decryption key for {browser_name}: {e}")
        return None


def decrypt_cookie_value(encrypted_value: bytes, key: bytes) -> str | None:
    """Decrypt a cookie value using the browser's encryption key."""
    if not encrypted_value:
        return None
    
    try:
        # Check for v20 App-Bound Encryption (Chrome 127+, Edge)
        # This encryption cannot be decrypted by external scripts
        if encrypted_value[:3] == b'v20':
            # Cannot decrypt v20 - App-Bound Encryption
            return None
        
        # Check for v10/v11 encryption (AES-256-GCM)
        if encrypted_value[:3] in (b'v10', b'v11'):
            try:
                from Cryptodome.Cipher import AES
            except ImportError:
                from Crypto.Cipher import AES
            
            nonce = encrypted_value[3:15]
            ciphertext = encrypted_value[15:-16]
            tag = encrypted_value[-16:]
            
            cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
            decrypted = cipher.decrypt_and_verify(ciphertext, tag)
            return decrypted.decode('utf-8')
        else:
            # Old DPAPI encryption (no prefix)
            try:
                import win32crypt
                decrypted = win32crypt.CryptUnprotectData(encrypted_value, None, None, None, 0)[1]
                return decrypted.decode('utf-8')
            except Exception:
                return None
                
    except Exception as e:
        logger.debug(f"Cookie decryption failed: {e}")
        return None


def extract_cookies_from_db(cookie_file: Path, key: bytes, domain_filter: str = "") -> dict[str, str]:
    """Extract and decrypt cookies from a Chromium cookie database."""
    cookies = {}
    temp_dir = None
    
    try:
        # Copy cookie file to temp directory (avoid lock issues)
        temp_dir = tempfile.mkdtemp()
        temp_cookie_path = os.path.join(temp_dir, "Cookies")
        shutil.copy2(cookie_file, temp_cookie_path)
        
        # Open database
        conn = sqlite3.connect(temp_cookie_path)
        cursor = conn.cursor()
        
        # Query cookies
        if domain_filter:
            cursor.execute(
                'SELECT host_key, name, encrypted_value FROM cookies WHERE host_key LIKE ?',
                (f'%{domain_filter}%',)
            )
        else:
            cursor.execute('SELECT host_key, name, encrypted_value FROM cookies')
        
        rows = cursor.fetchall()
        
        for host, name, encrypted_value in rows:
            decrypted = decrypt_cookie_value(encrypted_value, key)
            if decrypted:
                cookies[name] = decrypted
        
        conn.close()
        
    except Exception as e:
        logger.debug(f"Error extracting cookies: {e}")
    finally:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
    
    return cookies


def load_browser_cookies(domain_name: str = "", verbose: bool = True) -> tuple[dict, str]:
    """
    Load cookies from all supported browsers on Windows.
    
    Uses custom implementation that directly decrypts cookies,
    bypassing browser_cookie3 which has issues with newer browsers.
    
    Parameters
    ----------
    domain_name : str, optional
        Domain name to filter cookies by (e.g., ".google.com")
    verbose : bool, optional
        If True, print debug information
        
    Returns
    -------
    tuple[dict[str, dict], str]
        Tuple of (cookies dict, failure_reason string).
        failure_reason is empty if cookies were found successfully.
    """
    if platform.system() != "Windows":
        # Fall back to browser_cookie3 on non-Windows
        cookies = _load_browser_cookies_fallback(domain_name, verbose)
        return (cookies, "" if cookies else "Non-Windows platform, browser_cookie3 fallback failed.")
    
    cookies = {}
    browsers_checked = []
    failure_reason = ""
    
    # Check for required dependencies
    try:
        import win32crypt
    except ImportError:
        reason = "pywin32 not installed. Install with: pip install pywin32"
        if verbose:
            logger.warning(reason)
        return ({}, reason)
    
    try:
        from Cryptodome.Cipher import AES
    except ImportError:
        try:
            from Crypto.Cipher import AES
        except ImportError:
            reason = "pycryptodomex not installed. Install with: pip install pycryptodomex"
            if verbose:
                logger.warning(reason)
            return ({}, reason)
    
    browser_paths = get_chromium_browser_paths()
    
    if verbose:
        logger.debug(f"Found browsers: {list(browser_paths.keys())}")
    
    for browser_name in browser_paths:
        # Get decryption key
        key = get_decryption_key(browser_name)
        if not key:
            browsers_checked.append(f"{browser_name}(no key)")
            continue
        
        # Get all profiles
        profiles = get_chromium_profiles(browser_name)
        if not profiles:
            browsers_checked.append(f"{browser_name}(no profiles)")
            continue
        
        for cookie_file in profiles:
            profile_name = cookie_file.parent.parent.name
            
            try:
                browser_cookies = extract_cookies_from_db(cookie_file, key, domain_name)
                
                if browser_cookies:
                    # Check for Gemini-specific cookies
                    has_1psid = "__Secure-1PSID" in browser_cookies
                    has_1psidts = "__Secure-1PSIDTS" in browser_cookies
                    
                    if has_1psid:
                        # Found valid Gemini cookies
                        cookie_key = f"{browser_name}_{profile_name}"
                        cookies[cookie_key] = browser_cookies
                        
                        if verbose:
                            logger.debug(
                                f"[{browser_name}/{profile_name}] Found Gemini cookies! "
                                f"PSID: {has_1psid}, PSIDTS: {has_1psidts}"
                            )
                        browsers_checked.append(f"{browser_name}/{profile_name}(OK)")
                    else:
                        browsers_checked.append(f"{browser_name}/{profile_name}(no gemini)")
                else:
                    browsers_checked.append(f"{browser_name}/{profile_name}(empty)")
                    
            except Exception as e:
                browsers_checked.append(f"{browser_name}/{profile_name}(error)")
                if verbose:
                    logger.debug(f"Error reading {browser_name}/{profile_name}: {e}")
    
    if cookies:
        if verbose:
            logger.debug(f"Successfully loaded cookies from: {list(cookies.keys())}")
        return (cookies, "")
    
    # No cookies found - try v20 decryption and build failure reason
    try:
        from .v20_decrypt import check_dependencies, extract_edge_cookies
        deps = check_dependencies()
        
        if deps["can_decrypt"]:
            if verbose:
                logger.info("Attempting v20 decryption (running as admin)...")
            v20_cookies = extract_edge_cookies("google.com")
            
            if "__Secure-1PSID" in v20_cookies:
                cookies["edge_v20"] = v20_cookies
                if verbose:
                    logger.info(f"Successfully decrypted v20 cookies!")
                return (cookies, "")
        
        if not deps["is_admin"]:
            failure_reason = (
                f"Edge/Chrome 127+ uses App-Bound Encryption (v20). "
                f"You are NOT running ComfyUI as Administrator. "
                f"Either run ComfyUI as Administrator (right-click -> Run as administrator), "
                f"or use the 'manual' or 'cookie_file' auth method instead."
            )
        else:
            failure_reason = (
                f"Running as Admin but missing dependencies for v20 decryption. "
                f"Use 'cookie_file' or 'manual' auth method."
            )
    except ImportError:
        failure_reason = (
            f"Edge/Chrome 127+ uses App-Bound Encryption (v20) which cannot be decrypted externally. "
            f"Use 'cookie_file' or 'manual' auth method instead."
        )
    
    if verbose and failure_reason:
        logger.warning(f"No Gemini cookies extracted. Checked: {', '.join(browsers_checked)}. {failure_reason}")
    
    return (cookies, failure_reason)


def _load_browser_cookies_fallback(domain_name: str = "", verbose: bool = True) -> dict:
    """Fallback to browser_cookie3 for non-Windows platforms."""
    try:
        import browser_cookie3 as bc3
    except ImportError:
        if verbose:
            logger.warning("browser_cookie3 not installed")
        return {}
    
    cookies = {}
    
    for cookie_fn in [bc3.chrome, bc3.firefox, bc3.edge, bc3.safari]:
        browser_name = cookie_fn.__name__
        try:
            jar = cookie_fn(domain_name=domain_name)
            if jar:
                browser_cookies = {c.name: c.value for c in jar}
                if "__Secure-1PSID" in browser_cookies:
                    cookies[browser_name] = browser_cookies
                    if verbose:
                        logger.debug(f"[{browser_name}] Found Gemini cookies")
        except Exception as e:
            if verbose:
                logger.debug(f"[{browser_name}] Error: {e}")
    
    return cookies
