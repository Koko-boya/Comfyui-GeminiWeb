"""
v20 App-Bound Encryption Decryption for Edge/Chrome Browsers.

This module decrypts v20-encrypted cookies from Edge/Chrome by using
DPAPI with SYSTEM impersonation (requires admin).

For Edge:
- 2-stage decryption: SYSTEM DPAPI → User DPAPI → last 32 bytes = key

For Chrome:
- 3-stage decryption with additional AES-GCM/ChaCha20 based on flag

Dependencies:
- PythonForWindows: pip install PythonForWindows
- PyCryptodome: pip install pycryptodomex

IMPORTANT: Requires running as Administrator!

Based on:
- https://github.com/runassu/chrome_v20_decryption
- https://stackoverflow.com/questions/79489131
"""

import os
import io
import sys
import json
import struct
import ctypes
import sqlite3
import pathlib
import binascii
import platform
from contextlib import contextmanager
from typing import Optional

# Check for Windows
IS_WINDOWS = platform.system() == "Windows"

# Try to import dependencies (only on Windows)
HAS_PYTHONFORWINDOWS = False
AES = None
ChaCha20_Poly1305 = None

if IS_WINDOWS:
    try:
        import windows
        import windows.security
        import windows.crypto
        import windows.generated_def as gdef
        HAS_PYTHONFORWINDOWS = True
    except ImportError:
        pass

    try:
        from Cryptodome.Cipher import AES, ChaCha20_Poly1305
    except ImportError:
        try:
            from Crypto.Cipher import AES, ChaCha20_Poly1305
        except ImportError:
            pass


def is_admin() -> bool:
    """Check if running with administrator privileges."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except:
        return False


@contextmanager
def impersonate_lsass():
    """
    Impersonate lsass.exe to get SYSTEM privilege.
    Required for SYSTEM DPAPI decryption.
    """
    if not HAS_PYTHONFORWINDOWS:
        raise ImportError("PythonForWindows required: pip install PythonForWindows")
    
    original_token = windows.current_thread.token
    try:
        windows.current_process.token.enable_privilege("SeDebugPrivilege")
        proc = next(p for p in windows.system.processes if p.name == "lsass.exe")
        lsass_token = proc.token
        impersonation_token = lsass_token.duplicate(
            type=gdef.TokenImpersonation,
            impersonation_level=gdef.SecurityImpersonation
        )
        windows.current_thread.token = impersonation_token
        yield
    finally:
        windows.current_thread.token = original_token


def parse_chrome_key_blob(blob_data: bytes) -> dict:
    """Parse Chrome's key blob structure (for 3-stage decryption)."""
    buffer = io.BytesIO(blob_data)
    parsed_data = {}

    header_len = struct.unpack('<I', buffer.read(4))[0]
    parsed_data['header'] = buffer.read(header_len)
    content_len = struct.unpack('<I', buffer.read(4))[0]
    assert header_len + content_len + 8 == len(blob_data)
    
    parsed_data['flag'] = buffer.read(1)[0]
    
    if parsed_data['flag'] == 1 or parsed_data['flag'] == 2:
        # [flag|iv|ciphertext|tag]
        # [1byte|12bytes|32bytes|16bytes]
        parsed_data['iv'] = buffer.read(12)
        parsed_data['ciphertext'] = buffer.read(32)
        parsed_data['tag'] = buffer.read(16)
    elif parsed_data['flag'] == 3:
        # [flag|encrypted_aes_key|iv|ciphertext|tag]
        # [1byte|32bytes|12bytes|32bytes|16bytes]
        parsed_data['encrypted_aes_key'] = buffer.read(32)
        parsed_data['iv'] = buffer.read(12)
        parsed_data['ciphertext'] = buffer.read(32)
        parsed_data['tag'] = buffer.read(16)
    else:
        raise ValueError(f"Unsupported flag: {parsed_data['flag']}")

    return parsed_data


def decrypt_with_cng(input_data: bytes) -> bytes:
    """Decrypt using CNG (Microsoft Cryptography API) with Google Chrome's key."""
    ncrypt = ctypes.windll.NCRYPT
    hProvider = gdef.NCRYPT_PROV_HANDLE()
    provider_name = "Microsoft Software Key Storage Provider"
    status = ncrypt.NCryptOpenStorageProvider(ctypes.byref(hProvider), provider_name, 0)
    assert status == 0, f"NCryptOpenStorageProvider failed with status {status}"

    hKey = gdef.NCRYPT_KEY_HANDLE()
    key_name = "Google Chromekey1"
    status = ncrypt.NCryptOpenKey(hProvider, ctypes.byref(hKey), key_name, 0, 0)
    assert status == 0, f"NCryptOpenKey failed with status {status}"

    pcbResult = gdef.DWORD(0)
    input_buffer = (ctypes.c_ubyte * len(input_data)).from_buffer_copy(input_data)

    status = ncrypt.NCryptDecrypt(
        hKey, input_buffer, len(input_buffer), None, None, 0,
        ctypes.byref(pcbResult), 0x40  # NCRYPT_SILENT_FLAG
    )
    assert status == 0, f"1st NCryptDecrypt failed with status {status}"

    buffer_size = pcbResult.value
    output_buffer = (ctypes.c_ubyte * pcbResult.value)()

    status = ncrypt.NCryptDecrypt(
        hKey, input_buffer, len(input_buffer), None, output_buffer,
        buffer_size, ctypes.byref(pcbResult), 0x40
    )
    assert status == 0, f"2nd NCryptDecrypt failed with status {status}"

    ncrypt.NCryptFreeObject(hKey)
    ncrypt.NCryptFreeObject(hProvider)

    return bytes(output_buffer[:pcbResult.value])


def byte_xor(ba1: bytes, ba2: bytes) -> bytes:
    """XOR two byte arrays."""
    return bytes([_a ^ _b for _a, _b in zip(ba1, ba2)])


def derive_chrome_v20_master_key(parsed_data: dict) -> bytes:
    """Derive Chrome's v20 master key based on flag."""
    if AES is None:
        raise ImportError("PyCryptodome required: pip install pycryptodomex")
    
    if parsed_data['flag'] == 1:
        # AES-GCM with hardcoded key (Chrome v127+)
        aes_key = bytes.fromhex("B31C6E241AC846728DA9C1FAC4936651CFFB944D143AB816276BCC6DA0284787")
        cipher = AES.new(aes_key, AES.MODE_GCM, nonce=parsed_data['iv'])
    elif parsed_data['flag'] == 2:
        # ChaCha20-Poly1305 with hardcoded key
        if ChaCha20_Poly1305 is None:
            raise ImportError("ChaCha20_Poly1305 not available")
        chacha20_key = bytes.fromhex("E98F37D7F4E1FA433D19304DC2258042090E2D1D7EEA7670D41F738D08729660")
        cipher = ChaCha20_Poly1305.new(key=chacha20_key, nonce=parsed_data['iv'])
    elif parsed_data['flag'] == 3:
        # AES-GCM with CNG-decrypted key XORed with hardcoded key (Chrome v137+)
        xor_key = bytes.fromhex("CCF8A1CEC56605B8517552BA1A2D061C03A29E90274FB2FCF59BA4B75C392390")
        with impersonate_lsass():
            decrypted_aes_key = decrypt_with_cng(parsed_data['encrypted_aes_key'])
        xored_aes_key = byte_xor(decrypted_aes_key, xor_key)
        cipher = AES.new(xored_aes_key, AES.MODE_GCM, nonce=parsed_data['iv'])
    else:
        raise ValueError(f"Unsupported flag: {parsed_data['flag']}")

    return cipher.decrypt_and_verify(parsed_data['ciphertext'], parsed_data['tag'])


def get_edge_v20_key() -> Optional[bytes]:
    """
    Get the v20 master key for Edge browser.
    Edge uses simpler 2-stage DPAPI (no additional encryption).
    
    Requires running as Administrator!
    """
    if not HAS_PYTHONFORWINDOWS:
        raise ImportError("PythonForWindows required: pip install PythonForWindows")
    
    if not is_admin():
        raise PermissionError("Must run as Administrator for v20 decryption")
    
    user_profile = os.environ['USERPROFILE']
    local_state_path = rf"{user_profile}\AppData\Local\Microsoft\Edge\User Data\Local State"
    
    if not os.path.exists(local_state_path):
        return None
    
    # Read Local State
    with open(local_state_path, "r", encoding="utf-8") as f:
        local_state = json.load(f)

    app_bound_encrypted_key = local_state.get("os_crypt", {}).get("app_bound_encrypted_key")
    if not app_bound_encrypted_key:
        return None
    
    key_bytes = binascii.a2b_base64(app_bound_encrypted_key)
    if key_bytes[:4] != b"APPB":
        print("Warning: Key doesn't have APPB prefix")
        return None
    
    key_blob_encrypted = key_bytes[4:]
    
    # Stage 1: Decrypt with SYSTEM DPAPI
    with impersonate_lsass():
        key_blob_system_decrypted = windows.crypto.dpapi.unprotect(key_blob_encrypted)

    # Stage 2: Decrypt with user DPAPI
    key_blob_user_decrypted = windows.crypto.dpapi.unprotect(key_blob_system_decrypted)

    # For Edge: last 32 bytes are the master key
    v20_master_key = key_blob_user_decrypted[-32:]
    
    return v20_master_key


def get_chrome_v20_key() -> Optional[bytes]:
    """
    Get the v20 master key for Chrome browser.
    Chrome uses 3-stage decryption with additional AES-GCM/ChaCha20.
    
    Requires running as Administrator!
    """
    if not HAS_PYTHONFORWINDOWS:
        raise ImportError("PythonForWindows required: pip install PythonForWindows")
    
    if not is_admin():
        raise PermissionError("Must run as Administrator for v20 decryption")
    
    user_profile = os.environ['USERPROFILE']
    local_state_path = rf"{user_profile}\AppData\Local\Google\Chrome\User Data\Local State"
    
    if not os.path.exists(local_state_path):
        return None
    
    # Read Local State
    with open(local_state_path, "r", encoding="utf-8") as f:
        local_state = json.load(f)

    app_bound_encrypted_key = local_state.get("os_crypt", {}).get("app_bound_encrypted_key")
    if not app_bound_encrypted_key:
        return None
    
    key_bytes = binascii.a2b_base64(app_bound_encrypted_key)
    if key_bytes[:4] != b"APPB":
        return None
    
    key_blob_encrypted = key_bytes[4:]
    
    # Stage 1: Decrypt with SYSTEM DPAPI
    with impersonate_lsass():
        key_blob_system_decrypted = windows.crypto.dpapi.unprotect(key_blob_encrypted)

    # Stage 2: Decrypt with user DPAPI
    key_blob_user_decrypted = windows.crypto.dpapi.unprotect(key_blob_system_decrypted)
    
    # Stage 3: Parse blob and derive master key
    parsed_data = parse_chrome_key_blob(key_blob_user_decrypted)
    v20_master_key = derive_chrome_v20_master_key(parsed_data)
    
    return v20_master_key


def decrypt_v20_cookie(encrypted_value: bytes, master_key: bytes) -> Optional[str]:
    """
    Decrypt a v20 cookie value using AES-256-GCM.
    
    Format: v20 (3 bytes) + IV (12 bytes) + Ciphertext + Tag (16 bytes)
    After decryption, skip first 32 bytes (header).
    """
    if AES is None:
        raise ImportError("PyCryptodome required: pip install pycryptodomex")
    
    if len(encrypted_value) < 3 + 12 + 16:
        return None
    
    if encrypted_value[:3] != b"v20":
        return None
    
    try:
        iv = encrypted_value[3:15]
        tag = encrypted_value[-16:]
        ciphertext = encrypted_value[15:-16]
        
        cipher = AES.new(master_key, AES.MODE_GCM, nonce=iv)
        decrypted = cipher.decrypt_and_verify(ciphertext, tag)
        
        # Skip 32-byte header
        if len(decrypted) > 32:
            return decrypted[32:].decode("utf-8")
        else:
            return decrypted.decode("utf-8")
            
    except Exception as e:
        print(f"Cookie decryption failed: {e}")
        return None


def extract_edge_cookies(domain_filter: str = "") -> dict[str, str]:
    """
    Extract and decrypt v20 cookies from Edge.
    
    Returns a dict of {cookie_name: cookie_value}.
    Requires running as Administrator!
    """
    master_key = get_edge_v20_key()
    if not master_key:
        return {}
    
    user_profile = os.environ['USERPROFILE']
    cookie_db_path = rf"{user_profile}\AppData\Local\Microsoft\Edge\User Data\Default\Network\Cookies"
    
    if not os.path.exists(cookie_db_path):
        return {}
    
    cookies = {}
    
    try:
        # Open database in read-only mode
        con = sqlite3.connect(pathlib.Path(cookie_db_path).as_uri() + "?mode=ro", uri=True)
        cur = con.cursor()
        
        query = "SELECT host_key, name, CAST(encrypted_value AS BLOB) FROM cookies"
        if domain_filter:
            query += f" WHERE host_key LIKE '%{domain_filter}%'"
        
        cur.execute(query)
        rows = cur.fetchall()
        con.close()
        
        for host, name, encrypted_value in rows:
            if encrypted_value[:3] == b"v20":
                decrypted = decrypt_v20_cookie(encrypted_value, master_key)
                if decrypted:
                    cookies[name] = decrypted
                    
    except Exception as e:
        print(f"Error extracting cookies: {e}")
    
    return cookies


def check_dependencies() -> dict:
    """Check if all required dependencies are available."""
    return {
        "platform": platform.system(),
        "is_windows": IS_WINDOWS,
        "PythonForWindows": HAS_PYTHONFORWINDOWS,
        "PyCryptodome": AES is not None,
        "ChaCha20_Poly1305": ChaCha20_Poly1305 is not None,
        "is_admin": is_admin() if IS_WINDOWS else False,
        "can_decrypt": IS_WINDOWS and HAS_PYTHONFORWINDOWS and AES is not None and is_admin(),
    }


if __name__ == "__main__":
    print("=== v20 App-Bound Encryption Decryption ===\n")
    
    # Check dependencies
    deps = check_dependencies()
    print("Dependencies:")
    for name, available in deps.items():
        status = "✓" if available else "✗"
        print(f"  {status} {name}: {available}")
    
    if not deps["can_decrypt"]:
        print("\n⚠ Cannot decrypt v20 cookies!")
        if not deps["PythonForWindows"]:
            print("  Install: pip install PythonForWindows")
        if not deps["PyCryptodome"]:
            print("  Install: pip install pycryptodomex")
        if not deps["is_admin"]:
            print("  Run as Administrator!")
        sys.exit(1)
    
    print("\n--- Testing Edge v20 Decryption ---")
    try:
        cookies = extract_edge_cookies("google.com")
        print(f"Found {len(cookies)} Google cookies")
        
        # Show Gemini cookies
        for name in ["__Secure-1PSID", "__Secure-1PSIDTS"]:
            if name in cookies:
                print(f"  {name}: {cookies[name][:40]}...")
            else:
                print(f"  {name}: NOT FOUND")
                
    except Exception as e:
        print(f"Error: {e}")
