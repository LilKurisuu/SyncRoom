# SyncRoom v0.1

Self-hosted Plex watch rooms with a Nocturne-inspired black-and-white interface. Python/aiohttp, WebSockets and plain HTML/CSS/JavaScript. Newly authored from the original chat requirements, not a recovered archive. Original Nocturne design files were unavailable.

## Run

```sh
docker compose up -d --build
```

Open `http://YOUR-SERVER-IP:8765`. On Unraid, place this directory under an appdata folder and run Compose there using a Compose-capable setup, or build the Dockerfile and map port 8765. No persistent volume is required. Restarting clears rooms.

Local development (Python 3.13+):

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Watch together

1. Enter a name and create a room. Share the code or copied invite.
2. Each viewer connects to the same Plex server with their own token. The server URL must be reachable by SyncRoom and each browser.
3. The host opens a movie library and selects a movie.
4. The host controls play, pause and seeking. Guests follow. Click Enable playback if autoplay is blocked.

Use Plex's documented authentication-token procedure to obtain your token. Tokens stay in server memory for the connection and are returned only to their owning browser in its direct-play URL. They are not saved in source, shared room state or app access logs. Use HTTPS across untrusted networks. An HTTPS app requires an HTTPS Plex endpoint to avoid mixed-content blocking.

## Implemented

- Random room codes, participants, host handoff and empty-room cleanup.
- Host-only playback commands, late-join state and periodic drift correction.
- Plex validation, library browsing, movie selection and per-viewer direct playback.
- Responsive monochrome interface, Docker/Compose, MIT license and GitHub Actions.

## Limits

Use on a trusted LAN or private VPN. No accounts, room passwords, durable sessions or automatic reconnect. Room codes grant entry. The Plex URL feature permits private network destinations for self-hosting and can make requests from the server network; do not expose it to untrusted users.

Browser-compatible media only (commonly MP4/H.264/AAC). No transcoding, subtitles, alternate audio selection, TV-season browsing, Jellyfin or Plex OAuth yet. Movie libraries are the supported browsing path. Everyone needs access to the same media on the same Plex server. Synchronization is approximate; buffering and latency can produce drift.

Two-client integration tests cover guest denial, playback state, host handoff, cleanup, origin checks and mocked Plex selection/streaming. Actual Plex playback and Unraid deployment have not been verified.

## Checks

```sh
python -m unittest discover -s tests -v
```

GitHub Actions runs tests and builds the image. `GET /health` reports process health. Defaults to `0.0.0.0:8765`; `HOST` and `PORT` can override it.
