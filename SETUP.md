# Setup & Operations

## YouTube Authentication (Cookies)

YouTube blocks unauthenticated bot requests. Cookie auth is required.

### Environment variables (`.env`)

| Variable | Purpose |
|---|---|
| `YDL_COOKIES_FILE` | Path to an exported `cookies.txt` — takes priority over browser extraction |
| `YDL_BROWSER` | Browser to extract cookies from live (`firefox`, `chrome`, …) — local use only, does not work on headless servers |

### Initial setup (server)

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

1. Re-export with the command above
2. Upload via SCP
3. Run `!reloadcookies` in Discord — reloads without bot restart

### EJS signature solver

yt-dlp needs `yt-dlp[default]` (installs `yt-dlp-ejs`) and Node.js in PATH to solve YouTube signature challenges. Without it yt-dlp falls back to Deno only.

```bash
pip install "yt-dlp[default]"
```

`js_runtimes: {node: {}}` is already set in all ydl option dicts in `update_ydl()`.
