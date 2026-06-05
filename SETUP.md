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

Optional DM-Bridge variables (only for the AI dungeon master setup — leave default for normal use):

```
DM_BRIDGE_HOST=127.0.0.1
DM_BRIDGE_PORT=8765
DM_BRIDGE_SECRET=
```

| Variable | Purpose |
|---|---|
| `DISCORD_TOKEN` | Discord bot token (required) |
| `YDL_COOKIES_FILE` | Path to an exported `cookies.txt` — takes priority over browser extraction |
| `YDL_BROWSER` | Browser for live cookie extraction (`firefox`, `chrome`, …) — local only, not on headless servers |
| `LANGUAGE` | Bot language: `en` (default) or `de` |
| `DM_BRIDGE_HOST` | DM-Bridge HTTP host. `127.0.0.1` (default) = localhost path mode; LAN/Tailscale IP or `0.0.0.0` = remote byte mode |
| `DM_BRIDGE_PORT` | DM-Bridge HTTP port (default `8765`) |
| `DM_BRIDGE_SECRET` | Shared secret for DM-Bridge auth. Empty + `127.0.0.1` = no secret; **required** for any non-loopback host |

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

### Troubleshooting: Browser cookie extraction (`YDL_BROWSER`)

`cookiesfrombrowser` reads the cookie DB directly from a locally installed
browser. It's convenient for local dev but fragile — recommended only as a
fallback. If it fails, prefer the **`cookies.txt` route** below.

**`ERROR: Could not copy Chrome cookie database` (yt-dlp [#7271](https://github.com/yt-dlp/yt-dlp/issues/7271))**

Two causes:

1. **Chrome is still running** → the cookie DB is locked. Close Chrome
   *completely*, including background processes (system tray / `taskkill /F /IM
   chrome.exe` on Windows), then restart the bot.
2. **Chrome ≥ 127 (Windows): app-bound encryption** → cookies are encrypted so
   yt-dlp can't read them *even with Chrome closed*. There is no reliable fix
   for direct extraction — use the `cookies.txt` route below.

**Firefox extraction also fails (`could not find ... cookies database` / empty cookies)**

Usually one of:

- Not actually logged into YouTube in that Firefox profile.
- Multiple profiles — yt-dlp picks the default, which may not be the logged-in
  one. Target a specific profile: `YDL_BROWSER=firefox:<ProfileName>` (find it
  via `about:profiles`), e.g. `firefox:default-release`.
- Snap/Flatpak Firefox (Linux) stores the profile in a non-standard path
  yt-dlp can't locate → use the `cookies.txt` route.

### Recommended fallback: export `cookies.txt` (browser-independent)

Bypasses browser extraction entirely — works regardless of Chrome/Firefox
update breakage, and even while the browser is open:

1. Install the browser extension **"Get cookies.txt LOCALLY"**, open
   `youtube.com` while logged in, export `cookies.txt`.
2. In `.env` (this takes priority over `YDL_BROWSER`, so the browser is never
   touched):
   ```
   YDL_COOKIES_FILE=C:\path\to\cookies.txt
   ```
3. Run `!reloadcookies` in Discord — no restart needed.

> Mind a security note: `cookies.txt` contains your YouTube login session.
> Don't commit it or share it — keep it local / out of git (`.gitignore`).

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

## 6. Voice & Auto-Disconnect Behavior

The bot leaves the voice channel automatically in two cases:

- **No users left in the channel** → after 5 min (`AUTO_LEAVE_SECONDS = 300`).
- **No playback for a long time** → after 2 h of silence (`IDLE_LEAVE_SECONDS = 7200`).
  Discord drops idle (silent) voice connections with error **1006** after roughly
  80 min anyway; leaving on purpose avoids sitting on a dead connection.

If a 1006 drop happens *during* playback, discord.py auto-reconnects and a built-in
**reconnect watchdog** restarts the next track if playback got stuck — transparent,
no configuration needed. These reconnects are logged as a quiet INFO line instead of
a red traceback, so seeing one occasionally in `bot.log` is normal.
