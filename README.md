# IPTV Hive

An IPTV playlist that updates itself. It picks only the channels you want from several community playlists, tests each stream, and keeps the first one that works.

## Playlist URL

Use this link in your IPTV player:

```
https://raw.githubusercontent.com/YOUR-USERNAME/YOUR-REPO/main/playlist.m3u
```

## How it works

1. GitHub Actions runs daily at 02:00 UTC (or manually from the Actions tab).
2. For each line in `channels.txt`, it searches the sources in `sources.txt`, highest priority first.
3. It tests the matching streams and uses the first one that works.
4. Backup sources are only used if the earlier ones fail.
5. The result is saved to `playlist.m3u`, in the same order as `channels.txt`.

## Files

| File | Purpose |
|------|---------|
| `sources.txt` | Playlist URLs, one per line, highest priority first |
| `channels.txt` | Channels you want, one per line |
| `filter.py` | Builds the playlist |
| `.github/workflows/update-playlist.yml` | Daily automation |
| `playlist.m3u` | Output, updated automatically |

## Writing `channels.txt`

One line is one channel in your playlist.

| You write | What it does |
|-----------|--------------|
| `Aaj Tak Hindi` | Words in any order, extra words allowed. Finds "Aaj Tak HD Hindi" |
| `=Discovery` | Exact name only. Finds "Discovery", not "Discovery Turbo" |
| `Discovery -Turbo` | Excludes channels with that word |
| `Aaj Tak Hindi \| AajTak` | Alternative names for the same channel, tried in order |

Lines starting with `#` are ignored.

## Setup

1. Add all the files above to a new repo.
2. Go to Settings → Actions → General → Workflow permissions and choose **Read and write permissions**.
3. Open the Actions tab, select **Update playlist**, and tap **Run workflow**.

## Notes

- Streams are checked from GitHub's US servers, so streams that only work from some countries may be dropped.
- Channels with no working stream are left out and shown as `DEAD` in the Actions log.
- If nothing works at all, the old playlist is kept.
