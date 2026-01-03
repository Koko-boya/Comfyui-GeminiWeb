# Comfyui-GeminiWeb

Custom ComfyUI nodes for **Google Gemini** image generation and editing using the Gemini web interface.

![Gemini](https://img.shields.io/badge/Google-Gemini-blue?logo=google)
![ComfyUI](https://img.shields.io/badge/ComfyUI-Custom%20Node-green)

## Features

- **Text-to-Image** - Generate images from text using Gemini's native image model
- **Image-to-Image** - Edit/transform images with natural language
- **Vision Chat** - Chat with Gemini about images
- **Auto Authentication** - Supports browser cookie auto-detection (requires ComfyUI to be started with admin privileges)
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

### Option 1: Auto Cookies (Recommended)

1. Login to [gemini.google.com](https://gemini.google.com) in your browser
2. Use **"auto_cookies"** in the `Gemini Client Loader` node
3. The node will automatically extract cookies from your browser

### Option 2: Manual Cookies

1. Go to [gemini.google.com](https://gemini.google.com) and login
2. Open DevTools (F12) → Application → Cookies
3. Copy these cookie values:
   - `__Secure-1PSID` (required)
   - `__Secure-1PSIDTS` (optional)
4. Enter values in the `Gemini Client Loader` node

## Nodes

### Gemini Client Loader
Initialize authentication for the Gemini Web node.

| Input | Type | Description |
|-------|------|-------------|
| auth_method | ENUM | `auto_cookies` or `manual` |
| secure_1psid | STRING | Cookie (manual mode) |
| secure_1psidts | STRING | Cookie (optional) |
| proxy | STRING | HTTP proxy URL |
| timeout | INT | Connection timeout (seconds) |

**Output:** `GEMINI_CLIENT`

---

### Gemini Web
Unified node for all Gemini operations.

| Input | Type | Description |
|-------|------|-------------|
| client | GEMINI_CLIENT | From Client Loader |
| mode | ENUM | `text_to_image`, `image_to_image`, or `chat` |
| prompt | STRING | Text prompt |
| image | IMAGE | Input image (for image_to_image/chat) |
| model | ENUM | Gemini model to use |
| image_index | INT | Which image to return |

**Output:** `IMAGE`, `STRING` (response text)

#### Modes

- **text_to_image**: Generate images from text prompts
- **image_to_image**: Edit/transform an input image using text instructions
- **chat**: Chat with Gemini (text response, optional image input for vision)

> **Tip:** Include "generate" in your prompt for AI-generated images vs web search results.

## Example Workflows

### Text-to-Image Generation
```
[Gemini Client Loader] → [Gemini Web (text_to_image)] → [Preview Image]
```

### Image Editing
```
[Load Image] ─┬→ [Gemini Web (image_to_image)] → [Save Image]
              │
[Gemini Client Loader] ─┘
```

### Vision Chat
```
[Load Image] ─┬→ [Gemini Web (chat)] → [Text Output]
              │
[Gemini Client Loader] ─┘
```

## Available Models

- `unspecified` - Default model
- `gemini-3.0-pro` - Latest Pro model
- `gemini-2.5-pro` - Pro model
- `gemini-2.5-flash` - Fast model (default)

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Cookie expired" | Re-login to gemini.google.com and update cookies |
| "No images generated" | Try adding "generate" to your prompt |
| Import errors | Run `pip install -r requirements.txt` |
| Region restrictions | Image generation may not be available in all regions |
| "Event loop closed" | Restart ComfyUI |

## Credits

- Based on [Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) by HanaokaYuzu (vendored)
- ComfyUI Community

## License

This project is licensed under **AGPL-3.0** (same as the vendored Gemini-API library).

See [LICENSE](LICENSE) for details.

### Third-Party Code

The `gemini_webapi/` directory contains code from [Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) by HanaokaYuzu, licensed under AGPL-3.0.
