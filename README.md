<div align="center">

# reelish 🍿

**Save a movie. Play it by almost-name.**

[![checks](https://github.com/r0hin/reelish/actions/workflows/ci.yml/badge.svg)](https://github.com/r0hin/reelish/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.8%2B-3776AB?style=flat-square)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-black?style=flat-square)](LICENSE)

```text
/odysey  →  The Odyssey (2026)  →  ▶
```

fuzzy titles · persistent downloads · English subtitles · seekable streams

</div>

## Install

```sh
uv tool install git+https://github.com/r0hin/reelish
```

Or use `pipx install git+https://github.com/r0hin/reelish`. Python 3.8+ is required.

## Run

```sh
export REELISH_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
reelish serve --movies ~/Movies
```

Reelish listens on `127.0.0.1:8766`. Put it behind an HTTPS reverse proxy for
remote playback; a small [nginx + systemd example](examples/README.md) is included.

## Save

```sh
curl http://127.0.0.1:8766/downloads \
  -H "Authorization: Bearer $REELISH_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "The Odyssey (2026)",
    "videoUrl": "https://your-source.example/movie.mkv",
    "subtitleUrl": "https://your-source.example/english.srt"
  }'
```

The response includes a job `id`. Check progress with
`GET /downloads/<id>` and the same bearer token. Jobs survive restarts, partial transfers resume when the source supplies a reliable
validator, duplicate
requests share a job, and failed jobs can be retried by posting the same request.
The queue processes one movie at a time, with up to 20 pending downloads.

## Play

Open the server root in a browser for a live movie shelf, download progress, and
Play / Link / Subtitles / Delete actions. The Link menu can copy the URL or open
it in Outplayer; each card shows its added date and resolution. It refreshes every two seconds.

Paste a URL into IINA, VLC, or another network player:

```text
https://your-server/odysey
https://your-server/odyssey-1997
https://your-server/odysey/subtitles
```

Typos, accents, punctuation, Roman numerals, and sequel numbers are understood.
Include the release year for a remake; otherwise an equally close newer release
wins. Unrelated titles return 404. The first request redirects to a stable media
URL, so later seeks keep playing the same movie.

Completed movies stream from disk with HTTP byte-range seeking. While a download
is unfinished, requests proxy the original source, including seeks beyond the
saved bytes. The same media URL switches to disk when the file is complete.
Files you copy into the Movies folder are searchable too.

English subtitles are saved beside the video as `.en.srt`, `.en.vtt`, or `.en.ass`.
A player may need the `/subtitles` URL loaded separately. Matching searches your
saved and queued library; it doesn't discover or download movies from a title alone.

<details>
<summary>API & configuration</summary>

| Endpoint | Authentication | Purpose |
| :--- | :--- | :--- |
| `POST /downloads` | Bearer token | Queue a video + English subtitle |
| `GET /downloads/<id>` | Bearer token | Progress, status, and destination |
| `GET /resolve/<title>` | Bearer token | Top five matches and scores |
| `GET /health` | Bearer token | Service health |
| `DELETE /library/<id>` | Owner key | Move a movie and its subtitles to trash |
| `POST /trash/<id>/restore` | Owner key | Restore a trashed movie |
| `GET /` | Public | Live movie shelf |
| `GET /library` | Public | Titles, safe playback links, and progress |
| `GET /<title>` | Public | Match and play a movie |
| `GET /<title>/subtitles` | Public | Its English subtitle |

POST accepts optional `filename` and `subtitleDelay` (milliseconds). SRT/VTT delays
are applied to cue timestamps. ASS subtitles currently require zero delay.

| Setting | Default |
| :--- | :--- |
| `REELISH_TOKEN` | Required; at least 32 characters |
| `REELISH_MANAGE_TOKEN` | Optional separate owner key; defaults to the API token |
| `REELISH_MOVIES` / `--movies` | `~/Movies` |
| `REELISH_STATE` / `--state` | `~/.local/state/reelish` |
| `--host` | `127.0.0.1` |
| `--port` / `PORT` | `8766` |

The movie shelf, progress, and playback URLs are public; queue creation and administration require the token.
Only movies and subtitles under the media directory are served. Sources and
redirects must resolve to public HTTP(S) addresses; validated IPs are pinned for
the connection while HTTPS hostname verification remains enabled.

Delete moves files into a private `.reelish-trash` folder under Movies. The page
asks for the owner key and remembers it for the current tab. Undo restores the
movie; active downloads are cancelled and can be resumed when restored. A
restoration never overwrites an existing file. Trash is excluded from listing
and playback. The owner key cannot queue downloads when configured separately.

Resolution comes from source filenames when present, or a background `ffprobe`
inspection of the local video. Install FFmpeg to enable the latter (for example,
`sudo apt install ffmpeg` on Debian/Ubuntu or `brew install ffmpeg` on macOS).
Unidentified resolutions show as pending until enough media data is available.

Direct media URLs are supported. HLS playlists, DRM, and expired source links
aren't handled. Unfinished playback depends on the original source remaining
available. Keep enough disk space for the movies you queue.

</details>

<details>
<summary>Development</summary>

```sh
git clone https://github.com/r0hin/reelish
cd reelish
python3 -m venv .venv
. .venv/bin/activate
pip install -e . coverage build
coverage run --source=reelish -m unittest discover -s tests -v
coverage report -m
python -m build
```

Tests use temporary libraries and local HTTP sources. They exercise actual
transfers, interrupted downloads, concurrent seeks, subtitles, queue recovery,
and matching without relying on a live movie service.

</details>

---

Built with [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz) and Python.
Originally made for [Cinema](https://github.com/r0hin/cinema). [MIT](LICENSE).
