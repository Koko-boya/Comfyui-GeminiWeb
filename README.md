# Comfyui-GeminiWeb

Custom ComfyUI nodes for **Google Gemini** image generation and editing using the Gemini web interface.

![Gemini](https://img.shields.io/badge/Google-Gemini-blue?logo=google)
![ComfyUI](https://img.shields.io/badge/ComfyUI-Custom%20Node-green)
[![Registry](https://img.shields.io/badge/Comfy-Registry-green)](https://registry.comfy.org/publishers/kokoboy/nodes/comfyui-geminiweb)

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

### Option 1: Auto Cookies (Recommended)

1. Login to [gemini.google.com](https://gemini.google.com) in your browser
2. Use **"auto_cookies"** in the node
3. The node will automatically extract cookies from your browser

### Option 2: Manual Cookies

1. Go to [gemini.google.com](https://gemini.google.com) and login
2. Open DevTools (F12) → Application → Cookies
3. Copy these cookie values:
   - `__Secure-1PSID` (required)
   - `__Secure-1PSIDTS` (optional)
4. Enter values in the node inputs

## Node: GeminiWeb

Unified node for all Gemini operations.

### Inputs

| Input | Type | Description |
|-------|------|-------------|
| mode | ENUM | `text_to_image`, `image_to_image`, or `chat` |
| prompt | STRING | Text prompt |
| auth_method | ENUM | `auto_cookies` or `manual` |
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
| `unspecified` | Default model |
| `gemini-3.0-pro` | Latest Pro model |
| `gemini-2.5-pro` | Pro model |
| `gemini-2.5-flash` | Fast model (default) |

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Cookie expired" | Re-login to gemini.google.com and update cookies |
| "No images generated" | Try adding "generate" to your prompt |
| Import errors | Run `pip install -r requirements.txt` |
| Region restrictions | Image generation may not be available in all regions |
| "Event loop closed" | Restart ComfyUI |
| Filter not working | Each generation returns 2 images (watermarked + clean) |

## Credits

- Based on [Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) by HanaokaYuzu (vendored)
- ComfyUI Community

## License

This project is licensed under **AGPL-3.0** (same as the vendored Gemini-API library).

See [LICENSE](LICENSE) for details.

### Third-Party Code

The `gemini_webapi/` directory contains code from [Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) by HanaokaYuzu, licensed under AGPL-3.0.
