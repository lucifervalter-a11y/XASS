# VK Music Import — deploy notes

## 1. .env
```
vk_access_token=...          # Kate-Mobile / Boom token (audio.get works)
vk_user_id=123456789         # numeric VK id
vk_api_version=5.199
```
Get a token via `vkaudiotoken` (pip) or vodka2/vk-audio-token. The token must be
issued for a client that still has audio scope (Kate Mobile app id 2685278 or Boom).

## 2. Install
```
pip install -r requirements.txt
# new files: app/services/vk_audio_url_decoder.py, app/services/vk_music.py
```

## 3. Restart
```
sudo systemctl restart serverredus-backend
# or: pm2 restart xass
```

## 4. API
- `GET  /api/mini/music/vk/status` — check token
- `GET  /api/mini/music/vk/library?limit=50&offset=0` — preview VK library
- `POST /api/mini/music/vk/import`  body `{limit, offset, max_tracks}` — bulk import
- `POST /api/mini/music/vk/import-url` body `{url, title, artist}` — single track

## 5. Notes
- Encrypted URLs are decoded locally; no third-party proxy.
- Downloads stream to `.vk-ingest/` staging, then SHA-dedup via existing ingest_path.
- Rate-limit: VK allows ~3 req/s; importer spaces calls. Tune `limit` <= 200.
- If VK returns empty urls, the track is private/blocked — skipped with a reason.
