# Comfyui-GeminiWeb

Custom ComfyUI nodes for **Google Gemini** image generation and editing using the Gemini web interface.

![Gemini](https://img.shields.io/badge/Google-Gemini-blue?logo=google)
![ComfyUI](https://img.shields.io/badge/ComfyUI-Custom%20Node-green)

> **Security Warning**
> 
> This node accesses your browser cookies for authentication. Please be aware:
> - **Local use only** - Do not run on shared computers or public networks
> - **Cookie extraction** - The app scans your browser for Google session cookies
> - **Plain text storage** - Cookies may be stored in memory during session
> - **No SSL verification** - Requests may not verify SSL certificates
> 
> Use at your own risk. Only run this on your personal, private machine.

> **Note:** This is released as-is with no active maintenance planned. Pull Requests are welcome if you'd like to fix issues or improve the project!

## Features

- **Text-to-Image** - Generate images from text using Gemini's native image model
- **Image-to-Image** - Edit/transform images with natural language
- **Vision Chat** - Chat with Gemini about images
- **Multi-Image Input** - Support for up to 5 reference images
- **Watermark Filter** - Choose between watermarked, non-watermarked, or all images
- **Auto Authentication** - Supports browser cookie auto-detection
- **Self-Contained** - All dependencies bundled, no external API package needed

## Installation

### 1. Clone or Download

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/Koko-boya/Comfyui-GeminiWeb.git
```

Or download and extract to `ComfyUI/custom_nodes/Comfyui-GeminiWeb`

### 2. Install Dependencies

```bash
cd Comfyui-GeminiWeb
pip install -r requirements.txt
```

### 3. Restart ComfyUI

## Authentication Setup

Modern Chrome/Edge (v127+) uses **App-Bound Encryption (v20)** which requires special handling.

### Option 1: Manual (Recommended)

The simplest and most reliable method:

1. Login to [gemini.google.com](https://gemini.google.com)
2. Press **F12** → **Application** → **Cookies** → **gemini.google.com**
3. Copy `__Secure-1PSID` and `__Secure-1PSIDTS` values
4. Paste directly into the node's cookie inputs

### Option 2: Cookie File

Store cookies in a file for reuse:

1. Edit `gemini_cookies.txt` in the node folder:
   ```
   __Secure-1PSID=your_value_here
   __Secure-1PSIDTS=your_value_here
   ```
2. Use **"cookie_file"** in the node

### Option 3: Auto Cookies (Run as Administrator)

For automatic v20 cookie decryption (**Edge only tested**):

1. **Run ComfyUI as Administrator**
2. Login to gemini.google.com in **Edge**
3. **Close the browser completely** (important!)
   - Make sure the browser is not running in the background
   - Check Task Manager and end any Edge/Chrome processes
4. Use **"auto_cookies"** in the node

> **Why Admin?** Chrome/Edge 127+ use App-Bound Encryption (v20) which 
> requires SYSTEM-level access to decrypt. Currently only Edge is tested.

## Node: GeminiWeb

Unified node for all Gemini operations.

### Inputs

| Input | Type | Description |
|-------|------|-------------|
| mode | ENUM | `text_to_image`, `image_to_image`, or `chat` |
| prompt | STRING | Text prompt |
| auth_method | ENUM | `auto_cookies`, `cookie_file`, or `manual` |
| image_1 | IMAGE | Primary input image |
| image_2 | IMAGE | Optional reference image |
| image_3 | IMAGE | Optional reference image |
| image_4 | IMAGE | Optional reference image |
| image_5 | IMAGE | Optional reference image |
| model | ENUM | Gemini model to use |
| timeout | INT | API timeout (30-600 seconds) |
| image_filter | ENUM | `all`, `no_watermark`, or `watermarked` |
| cookie_1PSID | STRING | Cookie (manual mode) |
| cookie_1PSIDTS | STRING | Cookie (optional) |

### Outputs

| Output | Type | Description |
|--------|------|-------------|
| image | IMAGE | Generated/edited image(s) |
| response_text | STRING | Text response from Gemini |
| thinking | STRING | Model thinking/reasoning |

### Modes

- **text_to_image**: Generate images from text prompts
- **image_to_image**: Edit/transform input images using text instructions
- **chat**: Chat with Gemini (text response, optional image input for vision)

### Image Filter

| Filter | Description |
|--------|-------------|
| `all` | Return all generated images |
| `no_watermark` | Return only non-watermarked images (JPEG) |
| `watermarked` | Return only watermarked images (PNG) |

## Example Workflows

### Text-to-Image Generation
```
[GeminiWeb (text_to_image)] → [Preview Image]
```

### Image Editing with References
```
[Load Image 1] → image_1 ─┐
[Load Image 2] → image_2 ─┼→ [GeminiWeb (image_to_image)] → [Save Image]
[Load Image 3] → image_3 ─┘
```

### Vision Chat
```
[Load Image] → image_1 → [GeminiWeb (chat)] → [Text Output]
```

## Available Models

| Model | Description |
|-------|-------------|
| `unspecified` | Default model (uses Gemini's default) |
| `gemini-3-pro` | Pro model |
| `gemini-3-thinking` | Thinking model |
| `gemini-3-flash` | Fast model (default) |

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Cookie expired" | Re-login to gemini.google.com and update cookies |
| "v20 App-Bound Encryption" | Use `manual` method (recommended) or run as Admin with Edge |
| "No browser cookies found" | Use `manual` or `cookie_file` method (recommended) |
| "Cookie file not found" | Create `gemini_cookies.txt` with your cookies |
| "No images generated" | Try adding "generate" to your prompt |
| Import errors | Run `pip install -r requirements.txt` |
| Region restrictions | Image generation may not be available in all regions |
| v20 not decrypting | Run as Admin + close Edge + PythonForWindows installed |

### Reporting Issues

If you encounter problems, please open an issue with the prompt you used, or enable **debug_mode** in the node and attach the `debug_request.txt` file (found in the node's directory under `custom_nodes/Comfyui-GeminiWeb/`).

> ⚠️ **Do NOT share `debug_response.txt`** — it may contain your location and other personal details from Google.

## Credits

- Based on [Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) by HanaokaYuzu (vendored)
- v20 Cookie Decryption based on [chrome_v20_decryption](https://github.com/runassu/chrome_v20_decryption) by runassu
- ComfyUI Community

## License

This project is licensed under **AGPL-3.0** (same as the vendored Gemini-API library).

See [LICENSE](LICENSE) for details.

### Third-Party Code

The `gemini_webapi/` directory contains code from [Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) by HanaokaYuzu, licensed under AGPL-3.0.
