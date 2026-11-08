# photoframe-immich

Show photos from your [Immich](https://immich.app) library on a Seeed Studio **reTerminal E1002**
(7.3" Spectra 6 colour ePaper, ESP32-S3) running ESPHome.

```
Immich ──API──► photoframe-immich (Docker) ──/frame.png──► reTerminal E1002 (ESPHome, deep sleep)
                 pick photo → crop → enhance → dither to 6 inks
```

The ESP32 doesn't do any image processing. The server picks a photo and crops it to 800×480
(centred on detected faces). It then boosts the colour and dithers it to the panel's six inks
(black, white, red, yellow, green, blue). The frame wakes up, downloads the finished PNG,
refreshes the panel and goes back to sleep.

## 1. Run the server

Requirements: Docker, and Immich v1.133 or later (v2.x recommended).

1. In Immich, open **Account Settings → API Keys** and create a key. It needs the
   `asset.read`, `asset.view`, `album.read`, `person.read` and `tag.read` permissions,
   or you can grant all permissions.
2. Create and edit the config:
   ```sh
   cp server/config.example.yaml server/config.yaml
   ```
3. Start the service:
   ```sh
   IMMICH_URL=http://<immich-host>:2283 IMMICH_API_KEY=<key> docker compose up -d --build
   ```
4. Open `http://<server>:8080/`. The preview page shows:
   - the current photo as the panel will roughly render it, next to the original
   - a **Next photo** button
   - the exact album, person and tag names your Immich knows about, so you can copy them into `config.yaml`

After editing `config.yaml`, restart the container: `docker compose restart`.

## 2. Choosing which photos appear

Each wake, the server first picks one of your **sources** at random, using their `weight`s. It
then picks a random photo from that source, skipping recently shown ones (`history_size`).
Filters inside one source are combined with AND.

| Option | Immich concept | Notes |
|---|---|---|
| `albums: [..]` | Albums you own or that are shared with you | Photos from **any** of the listed albums. |
| `people: [..]` | Faces Immich recognised and you named (Explore → People) | `people_match: any` (default) or `all` (only photos where everyone listed appears). Name people in Immich first. |
| `favorites: true` | The ★ heart on a photo | |
| `tags: [..]` | Tags (enable in Account Settings → Features → Tags) | Nested tags can use their full path, e.g. `Travel/Hawaii`. |
| `min_rating: 4` | Star ratings (enable in Account Settings → Features → Rating) | 4 means 4 or 5 stars. |
| `smart_query: "..."` | Immich's CLIP smart search: the same as typing in the search bar | Results are ranked by relevance, so keep `max_candidates` small (100–200). |
| `taken_after` / `taken_before` | EXIF capture date | `YYYY-MM-DD` |
| `city` / `state` / `country` | Reverse-geocoded GPS location | Must match Immich's spelling exactly (see Explore → Places). |

Videos and archived photos are always excluded.

Global settings:
- **`orientation`**: the panel is landscape.
  - `landscape_only` skips portrait photos.
  - `landscape_preferred` (default) shows them less often (`portrait_weight`).
  - Portrait photos are shown whole over a blurred background (`render.portrait_mode: blur_fill`), or cropped (`crop`).
- **`render.saturation` / `contrast`**: Spectra 6 inks are muted, so colour is boosted by default. Tune these with the preview page and the real panel side by side.
- **`render.measured_palette`**: the colours the inks really show, used for dithering. If skin tones or skies look off, photograph the panel showing solid colours and adjust these values.

Search results are cached for `candidate_cache_minutes`. After adding photos to an album,
click **Re-query Immich** on the preview page to pick them up sooner.

## 3. Flash the frame

Requires ESPHome **2025.11 or newer**, which includes the `Seeed-reTerminal-E1002` display preset.

```sh
cd esphome
cp secrets.example.yaml secrets.yaml   # fill in Wi-Fi + API key
# edit server_url in photoframe.yaml
esphome run photoframe.yaml            # first flash over USB-C
```

[reterminal-e1002.yaml](esphome/reterminal-e1002.yaml) holds only the hardware definition. The
behaviour is in [photoframe.yaml](esphome/photoframe.yaml).

### What the frame does

1. Wakes on its timer or when the **right** button is pressed.
2. Fetches `/config.json`. This reports the battery level to the server and gets the sleep settings.
3. Downloads `/frame.png`. Each request advances to the next photo.
4. Refreshes the panel (about 20–30 s), then deep-sleeps for `device.sleep_minutes`.

If Wi-Fi or the server fails, the frame keeps showing the current picture and retries after
`retry_minutes`. When the battery is below `low_battery_percent`, the frame draws a small
battery icon and sleeps for `low_battery_sleep_minutes` instead.

Sleep timing is controlled from the server's `config.yaml` (`device:` section), so it can be
changed without reflashing. The frame picks up the change on its next wake.

### Staying awake (USB power, OTA updates, logs)

A sleeping frame can't receive OTA updates or show logs. Any of these keeps it awake:
- Hold the **left** button while it boots (press reset, or wake it with the right button).
- Turn on the **Stay awake** switch in Home Assistant (the frame sees it on its next wake).
- Set `device.stay_awake: true` on the server.

While awake, the frame refreshes every `sleep_minutes`. The **green** button, or the
**Next photo** button in Home Assistant, shows the next photo straight away. Press the left
button again, or turn the switch off, and the frame goes back to sleeping.

## Server endpoints

| Endpoint | Purpose |
|---|---|
| `GET /frame.png` | Next photo as an 800×480 PNG in the six pure ink colours. `?advance=false` returns the current one again. |
| `GET /config.json?battery=NN` | Device timing settings; also records the battery level |
| `POST /next` | Advance now (e.g. from a Home Assistant automation) |
| `GET /status` | Current and upcoming photo, source candidate counts, battery level, last error |
| `GET /catalog` | Album, person and tag names available in Immich |
| `POST /refresh-sources` | Clear cached search results |
| `GET /` | Preview page |

## Development

```sh
cd server
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
# try the dithering on any local photo:
.venv/bin/python -m app.render photo.jpg out.png --simulate sim.png
# run against your Immich:
IMMICH_URL=... IMMICH_API_KEY=... STATE_DIR=./data .venv/bin/uvicorn app.main:app --reload --port 8080
```
