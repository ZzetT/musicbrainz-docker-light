# Mini PC setup: web service API only

This setup runs a MusicBrainz mirror on a small host (mini PC, NUC, home
server) for the sole purpose of serving the MusicBrainz web service API
(`/ws/2/`) to other applications on the local network, such as
[DroppedNeedle](https://github.com/DroppedNeedle/DroppedNeedle) and
[SoulSync](https://github.com/Nezreka/SoulSync).
The website (HTML pages) is not served.

## What the client applications need

Both applications were checked for the requests they send
(DroppedNeedle `backend/repositories/musicbrainz_*.py`,
SoulSync `core/musicbrainz_client.py`):

| Request kind | Examples | Served by |
| --- | --- | --- |
| Lookup | `/ws/2/artist/<mbid>?inc=aliases+url-rels+tags`, `/ws/2/release/<mbid>?inc=recordings+…`, `/ws/2/recording/<mbid>`, `/ws/2/release-group/<mbid>`, `/ws/2/isrc/<isrc>`, `/ws/2/url?resource=…`, `/ws/2/genre/all` | Postgres |
| Browse | `/ws/2/release-group?artist=<mbid>`, `/ws/2/release?release-group=<mbid>`, `/ws/2/release?label=<mbid>` | Postgres |
| Search | `/ws/2/{artist,release,release-group,recording,label}?query=…` (including `arid:` and `tag:` queries), `/ws/2/series?query=…` | Solr |
| Account | `/ws/2/collection/…`, tag/rating submissions | musicbrainz.org only |

So Postgres and the web service are always needed, and Solr is needed for
six collections out of fifteen: `artist`, `label`, `recording`, `release`,
`release-group` and `series`. Nothing uses the website, the search indexer,
or the `annotation`, `area`, `cdstub`, `event`, `instrument`, `place`,
`tag`, `url` and `work` search collections.

Both applications let you point them at your own server:
DroppedNeedle has an API URL setting (`…/ws/2`), SoulSync has
`musicbrainz.base_url` (or `SOULSYNC_MUSICBRAINZ_BASE_URL`).
Use `http://<mini-pc>:5000/ws/2`. As a local mirror has no rate limit,
you can also lower their request interval.

## What the `mini-pc` profile changes

| Component | Default setup | `mini-pc` profile |
| --- | --- | --- |
| Website | Webpack recompiles resources at start (when host/port changed), Node.js React renderer always running | Neither: `light/musicbrainz/start-api-only.sh` starts the Perl web service workers only |
| Web workers | 10 `plackup` workers | 3 (`MUSICBRAINZ_SERVER_PROCESSES`) |
| Postgres | `shared_buffers=2048MB`, 2 GB `/dev/shm` | `shared_buffers=512MB`, `jit=off`, 1 parallel worker per query, SSD costs, 256 MB `/dev/shm` (all overridable) |
| Solr | 2 GB heap, all 15 collections (68 GB of archives) | 1 GB heap (`SOLR_HEAP`), only the collections in `MB_SEARCH_CORES` |
| Search indexer (sir) | Always running | Disabled (profile `indexer`); indexes come from the pre-built archives instead |
| Valkey cache | Unbounded, persisted to disk | 256 MB LRU (`VALKEY_MAXMEMORY`), not persisted |
| Port 5000 | MusicBrainz Server (website + API) | nginx gateway: `/ws/2/` only, everything else returns 404 |

The gateway also forwards to musicbrainz.org, rate limited to 1 request
per second, the requests the mirror cannot answer: searches in collections
that are not loaded locally, and account requests (collections, any
non-GET request). Set `MB_UPSTREAM_HOST=` (empty) to answer them with 503
instead and never contact musicbrainz.org.

### Resource estimates

Measured on an empty database (idle, after a few hundred API requests):

| Service | Default | `mini-pc` |
| --- | --- | --- |
| musicbrainz | 704 MiB | 442 MiB |
| search | 2.34 GiB | 1.28 GiB |
| indexer | running | not running |
| gateway | – | 6 MiB |

With the full database, memory grows with the cache sizes set above: expect
roughly 3–4 GB of RAM in use for the `mini-pc` profile versus 8 GB or more
for the default setup, plus whatever the OS uses as page cache (more is
faster, it is not required). A host with 8 GB of RAM should be enough;
16 GB gives the page cache room for the busiest parts of the database and
the search indexes. These full-data figures are estimates, not measurements.

Disk (estimates from the upstream README and the archive sizes of
2026-10-03):

* Postgres database: about 100 GB, plus about 7 GB of dumps during import
  (can be deleted afterwards).
* Search, default `MB_SEARCH_CORES`: about 59 GB of archives
  (recording alone is 49 GB) and a similar amount for the loaded index.
  `refresh-search-cores` loads one collection at a time and deletes each
  archive right away, so the archives never all sit on disk at once.
* Search without `recording` (see below): about 10 GB.

The upstream default needs about 350 GB with search; plan for 250 GB with
the default `mini-pc` collections, or about 150 GB without `recording`.

## Installation

1. Select the profile (MusicBrainz mirror, API only, with the gateway):

   ```bash
   admin/configure with default mini-pc
   docker compose build
   ```

2. Create the database from the latest full dumps:

   ```bash
   docker compose run --rm musicbrainz createdb.sh -fetch
   ```

   Once it succeeded, you can free the dumps' disk space:

   ```bash
   docker compose run --rm musicbrainz bash -c 'rm -rf /media/dbdump/*'
   ```

3. Start the services and load the search collections:

   ```bash
   docker compose up -d
   docker compose exec search refresh-search-cores
   ```

4. Enable replication to keep the database up to date, see
   [Enable replication](../README.md#enable-replication):

   ```bash
   admin/set-replication-token
   admin/configure add replication-token replication-cron
   docker compose up -d
   ```

5. Search indexes are not replicated. Refresh them weekly with a host
   cron job, for example on Sunday at 1 am:

   ```crontab
   0 1 * * 7 YOUR_USER_NAME cd ~/musicbrainz-docker-light && /usr/bin/docker compose exec -T search refresh-search-cores
   ```

Check that the API answers with:

```bash
curl 'http://localhost:5000/ws/2/artist/056e4f3e-d505-4dad-8ec1-d04f521cbb56?fmt=json'
```

## Going lighter: recording search from musicbrainz.org

The `recording` collection is 49 of the 68 GB of search archives and the
largest consumer of Solr memory and page cache. Without it, recording
searches are forwarded by the gateway to musicbrainz.org (1 request per
second), while lookups and browses, including recording lookups, stay local.
Add to `.env`:

```bash
MB_SEARCH_CORES="artist label release release-group series"
```

Then apply with `docker compose up -d`. If `recording` was loaded before,
drop its index with `docker compose exec search delete-indexed-documents recording`.

## Tuning

All the settings below go in `.env`, then `docker compose up -d`:

| Variable | Default | Notes |
| --- | --- | --- |
| `MUSICBRAINZ_SERVER_PROCESSES` | `3` | Concurrent API requests; each worker takes 50–300 MB |
| `POSTGRES_SHARED_BUFFERS` | `512MB` | Up to 25% of RAM if you have it to spare |
| `POSTGRES_EFFECTIVE_CACHE_SIZE` | `2GB` | About half of the RAM |
| `POSTGRES_WORK_MEM` | `8MB` | |
| `POSTGRES_MAX_CONNECTIONS` | `50` | |
| `SOLR_HEAP` | `1g` | Raise to `1500m` or `2g` if Solr logs out-of-memory errors |
| `MB_SEARCH_CORES` | `artist label recording release release-group series` | Local search collections |
| `MB_UPSTREAM_HOST` | `musicbrainz.org` | Empty to never forward requests |
| `MB_UPSTREAM_RATE` | `1r/s` | Keep within the [MusicBrainz rate limit](https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting) |
| `VALKEY_MAXMEMORY` | `256mb` | |

To build the search indexes from the database instead of downloading them,
start the indexer with `docker compose --profile indexer up -d indexer`;
it needs much more CPU and memory than the rest of this setup.
