# Setup & Operation

## 1. Requirements

- Python 3.10+
- FFmpeg in PATH
- Node.js in PATH (for YouTube signature solver)

## 2. Installation

```bash
git clone https://github.com/Pr0degie/musicbot.git
cd Musicbot
python -m venv venv

# Windows:
venv\Scripts\activate
# Linux / macOS:
source venv/bin/activate

pip install -r requirements.txt
```

## 3. Configuration (`.env`)

Create a `.env` file in the project folder:

```
DISCORD_TOKEN=your_token_here
```

Optional cookie variables for YouTube authentication (→ section 4):

```
YDL_COOKIES_FILE=/path/to/cookies.txt
YDL_BROWSER=firefox
```

Optional language setting:

```
LANGUAGE=en
```

| Variable | Purpose |
|---|---|
| `DISCORD_TOKEN` | Discord bot token (required) |
| `YDL_COOKIES_FILE` | Path to an exported `cookies.txt` — takes priority over browser extraction |
| `YDL_BROWSER` | Browser for live cookie extraction (`firefox`, `chrome`, …) — local only, not on headless servers |
| `LANGUAGE` | Bot language: `en` (default) or `de` |

## 4. YouTube Authentication (Cookies)

YouTube blocks unauthenticated bot requests. Cookie auth is required.

### Server setup (recommended)

Export cookies from a logged-in browser and upload them:

```bash
yt-dlp --cookies-from-browser firefox --cookies cookies.txt --skip-download <any-youtube-url>
scp cookies.txt user@server:/path/to/MusicBot/cookies.txt
```

Set in `.env`:
```
YDL_COOKIES_FILE=/path/to/MusicBot/cookies.txt
```

### Cookie renewal

Cookies last roughly 1–3 months. When YouTube starts blocking again:

1. Re-export cookies (command above)
2. Upload via SCP
3. Run `!reloadcookies` in Discord — reloads without restarting the bot

### EJS Signature Solver

yt-dlp requires Node.js in PATH and `yt-dlp[default]` for YouTube signature challenges:

```bash
pip install "yt-dlp[default]"
```

`js_runtimes: {node: {}}` is already configured in all ydl instances in `update_ydl()`.

## 5. Start

```bash
python main.py
```
