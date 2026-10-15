# SyncRoom v0.3

Self-hosted Plex watch rooms with a Nocturne-inspired black-and-white interface. Python/aiohttp, WebSockets and plain HTML/CSS/JavaScript. Newly authored from the original chat requirements, not a recovered archive. Original Nocturne design files were unavailable.

## Run

```sh
docker compose up -d --build
```

Open `http://YOUR-SERVER-IP:8765`. On Unraid, place this directory under an appdata folder and run Compose there using a Compose-capable setup, or build the Dockerfile and map port 8765. The named Docker volume stores encrypted sign-in sessions. Restarting clears rooms but preserves remembered sign-ins. Do not delete the volume during upgrades.

Local development (Python 3.13+):

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Watch together

1. Click **Sign in with Plex** and approve SyncRoom on Plex’s website. Your Plex display name becomes your participant name automatically.
2. Create a room and share the code or invite; your friends sign in with Plex, then join using that code. A sole available server connects automatically; use Your Plex server to choose when there are several. Everyone should select the same server.
3. The host opens a movie library and selects a movie.
4. The host controls play, pause and seeking. Guests follow. Click Enable playback if autoplay is blocked.

### Remembered sign-in

Keep me signed in is enabled by default. A browser session lasts 30 days from the latest page visit. Uncheck it before signing in for a visit-only connection. The app stores only an opaque HTTP-only, SameSite=Strict cookie in the browser; it does not store Plex tokens in localStorage. SQLite stores the encrypted account token and server choice. A separate Fernet key lives in the same private data directory. Sign out invalidates the browser session and signs out other open tabs sharing that cookie. Clearing cookies or Plex revoking access requires another sign-in. To revoke SyncRoom at Plex, remove it from your Plex account’s Authorized Devices.

The app uses Plex’s documented hosted PIN authorization flow. Passwords are entered on `app.plex.tv`, never in SyncRoom. Account tokens stay on the server; the selected server’s media token is supplied only to that viewer’s browser for direct playback. The media token can consequently be visible to that viewer in browser developer tools. Sign-in and server tokens are never included in room state or committed to source.

By default, data is stored in `data/` for local Python development and `/data` in Docker. Back up both `session.key` and `sessions.sqlite3` together, and protect the entire directory: someone who obtains both can decrypt credentials. Rooms remain ephemeral. An app restart preserves the login, not room membership. No Plex secrets or encryption keys ship with the source.

Use HTTPS across untrusted networks. If terminating HTTPS at a reverse proxy, preserve WebSocket upgrades and the original Host header, and set `SYNCROOM_SECURE_COOKIE=1` in the container environment so browser cookies require HTTPS. Do not set it for plain HTTP LAN testing. An HTTPS page needs an HTTPS Plex connection; server choices are filtered accordingly.

### Unraid Docker setup

The image supports Linux AMD64 (typical Unraid servers) and ARM64. Standard Docker Compose uses a named data volume and runs as UID 10001. The supplied Unraid overlay uses Unraid’s usual nobody/users IDs, `99:100`, and binds `/mnt/user/appdata/syncroom` to `/data`.

On the Unraid terminal, clone the repository somewhere outside the private appdata directory, then prepare storage and start:

```sh
mkdir -p /mnt/user/appdata/syncroom
chown 99:100 /mnt/user/appdata/syncroom
chmod 700 /mnt/user/appdata/syncroom
docker compose -f compose.yaml -f compose.unraid.yaml up -d --build
```

Run the Compose command from the cloned source folder using a Compose-capable Unraid setup. Open `http://UNRAID-IP:8765`. For an alternate appdata path, set `SYNCROOM_APPDATA` before running Compose. If your Unraid ownership differs, change the overlay’s user IDs to match the directory owner.

For Unraid’s Docker GUI, first build the local image (`docker build -t syncroom:local .`), use repository `syncroom:local`, bridge networking, container port 8765 to host port 8765, and a writable `/data` path mapped to the prepared appdata directory. Set Extra Parameters to `--user 99:100` when using that directory. There is no published registry image yet; local builds or Compose are the supported installation paths.

For updates, rebuild/recreate the container while preserving `/data`; never run `docker compose down -v` unless intentionally deleting remembered sign-ins. LAN connections to Plex must be reachable from both the container and the viewers’ browsers. Do not use `localhost` to refer to another machine’s Plex server.

## Implemented

- Dedicated room header, shareable invites, participant avatars and host badges.
- One participant per Plex account, with a count for multiple tabs. Host controls work across that account’s tabs.
- Refresh and invite links rejoin automatically after sign-in. A disconnected account has 20 seconds to reconnect before removal and host handoff. Leaving explicitly takes effect immediately.
- Random room codes, host handoff and empty-room cleanup.
- Host-only playback commands, late-join state and periodic drift correction.
- Plex hosted sign-in, remembered sessions, account display names, account server discovery, movie browsing and per-viewer direct playback.
- Responsive monochrome interface, Docker/Compose, MIT license and GitHub Actions.

## Limits

Use on a trusted LAN or private VPN. No room passwords. Plex sign-in is required to create or join rooms; the room code grants access to an existing room. The Plex URL feature permits private network destinations for self-hosting and can make requests from the server network; do not expose it to untrusted users.

Browser-compatible media only (commonly MP4/H.264/AAC). No transcoding, subtitles, alternate audio selection, TV-season browsing or Jellyfin yet. Movie libraries are the supported browsing path. Everyone needs access to the same media on the same Plex server. Synchronization is approximate; buffering and latency can produce drift.

Integration tests cover room behavior, hosted sign-in with mocked Plex APIs, polling limits, token isolation, encryption, remembered-login restoration, expiry and cross-tab sign-out. Actual Plex playback and Unraid deployment have not been verified.

## Checks

```sh
python -m unittest discover -s tests -v
```

GitHub Actions runs tests and builds the image. `GET /health` reports process health. Defaults to `0.0.0.0:8765`; `HOST` and `PORT` can override it.
