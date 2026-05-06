# astrbot_plugin_animewifex Architecture

## Entry Point

`main.py` is the AstrBot adapter. It owns command registration, event parsing, message replies, and orchestration between services.

It should not grow new domain algorithms directly. New reusable behavior belongs in `services/`.

## Services

`services/translation.py`

- Owns `en_cache.json`.
- Normalizes legacy `en` / `alt` entries and new full translation profiles.
- Provides callbacks used by `HentaiSearcher`.

`services/character_resolver.py`

- Resolves user-entered names into candidate characters.
- Searches Bangumi, AniList, and VNDB.
- Returns normalized `{name, source, thumb_url}` candidates.

`services/review.py`

- Owns review status constants and labels.
- Keeps the add-wife review flow from depending on scattered magic strings.

`services/image_fetcher.py`

- Fetches candidate review images.
- Owns Pixiv, custom sources, booru, VNDB, Getchu, DLsite, and thumbnail fallback logic.
- Uses source-aware image ordering: exact character+source tags first, VNDB official art next, broad character tags after that, and source covers only as a last resort.
- Depends on a plain async translation callback instead of AstrBot.

`services/github_publisher.py`

- Owns GitHub branch/file/list.txt/PR operations.
- Uses translation profiles for ASCII-safe branch names when available.
- Keeps manual-upload PR creation separate from image-backed PR creation.

`services/retention.py`

- Owns draw streaks, daily draw counters, album summaries, and ranking row generation.
- Keeps retention text and progress calculations testable without AstrBot.

## Migration Rules

- Keep AstrBot-specific objects (`AstrMessageEvent`, `MessageChain`, `Plain`, `Image`, `At`) in `main.py`.
- Service classes should accept plain strings, dicts, bytes, and config values.
- Each service should be importable and testable without AstrBot.
- `main.py` may keep compatibility wrappers during migration, but new code should call services directly.

## Target Split

Next services to extract:

- `services/review_flow.py`: add-wife state transitions and admin action handling.
- `services/wife_pool.py`: list cache refresh, draw de-duplication, and image path validation.
