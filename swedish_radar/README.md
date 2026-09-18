# Swedish Radar

Find new releases on Spotify that have a Swedish connection, along any of three
independent criteria:

1. **By Swedish artists** — the performing artist is Swedish.
2. **By Swedish independent record labels** — the release came out on a Swedish
   indie, not a major.
3. **Written primarily by Swedish songwriters** — most of the credited
   composers/lyricists are Swedish.

Pick one, two or all three. A release matches if it satisfies **any** selected
criterion, and every match carries the evidence it was built from.

No runtime dependencies — the whole thing is Python standard library.

---

## Quick start

```bash
# 1. Credentials (https://developer.spotify.com/dashboard - Client Credentials, no redirect URI)
cp .env.example .env && $EDITOR .env

# 2. Check the setup
python3 -m swedish_radar doctor

# 3. Find releases from the last two weeks
python3 -m swedish_radar discover --criteria artist,label --days 14

# 4. Or use the web UI
python3 -m swedish_radar serve
```

Installing it puts a `swedish-radar` command on your PATH:

```bash
pip install -e .
swedish-radar discover --format markdown -o digest.md
```

---

## Commands

| Command | What it does |
|---|---|
| `discover` | Run a discovery pass and print/write the results |
| `explain <album>` | Show every signal for one album (id or Spotify URL) |
| `render` | Re-render a saved run in another format, offline |
| `serve` | Web UI on `http://127.0.0.1:8000` |
| `labels list` / `check` / `add` | Inspect and extend the curated Swedish label list |
| `cache clear` / `purge` | Manage the response cache |
| `doctor` | Verify configuration |

### `discover`

```bash
swedish-radar discover \
  --criteria artist,label,writer \   # any combination; default all three
  --markets SE \                     # comma-separated ISO country codes
  --days 14 \                        # release window
  --min-confidence 0.55 \            # match threshold, 0-1
  --writer-majority 0.5 \            # share of writers that must be Swedish
  --format markdown \                # text | json | csv | markdown
  --out digest.md \
  --save                             # also record the run in the results DB
```

### `explain`

```bash
$ swedish-radar explain https://open.spotify.com/album/XXXXXXXXXXXXXXXXXXXXXX

Vinterhav — Nattens Ljus
  2026-09-15 · album · label: Adrian Recordings

  [MATCH ] Swedish artist (0.99)
           Vinterhav is Swedish (confirmed in MusicBrainz).
           +0.95 MusicBrainz places Vinterhav in Sweden (Göteborg) [musicbrainz]
           +0.80 Vinterhav: explicitly Swedish genre: swedish indie pop [spotify]
           +0.40 2/2 ISRCs registered in Sweden (SE prefix) [spotify]

  [MATCH ] Swedish independent label (0.97)
           Released on Adrian Recordings (Swedish independent, Malmö).
           +0.95 'Adrian Recordings' (Malmö) is on the curated list [reference]

  [no    ] Swedish songwriters (0.00)
           MusicBrainz has no songwriter credits for this release yet.
```

### `render`

`discover --format json` output (and anything saved with `--save`) is a
round-trippable interchange format, so a single pass over the APIs can produce
every format you need:

```bash
swedish-radar discover --format json --out radar/latest.json --save
swedish-radar render --input radar/latest.json --format markdown --out radar/latest.md
swedish-radar render --run 12 --format csv          # from the results database
```

---

## How each criterion is decided

Full detail lives in [`docs/methodology.md`](../docs/methodology.md). In short:

### 1. Swedish artist

| Signal | Weight | Source |
|---|---|---|
| MusicBrainz country = SE, artist matched via its Spotify link | +0.95 | MusicBrainz |
| MusicBrainz country = SE, artist matched by name search | +0.75 | MusicBrainz |
| MusicBrainz country ≠ SE | −0.90 | MusicBrainz |
| Spotify genre tag starts with `swedish`/`svensk` | +0.80 | Spotify |
| Sweden-only genre (`dansband`, `trallpunk`, `gothenburg metal`, …) | +0.70 | Spotify |
| ≥ half the ISRCs have the `SE` country prefix | +0.40 | Spotify |
| Released on a Swedish independent label | +0.45 | derived |
| Available in Sweden only | +0.35 | Spotify |
| Titles use Swedish-distinctive words | ≤ +0.45 | heuristic |

A collaboration matches on its Swedish member, and the per-artist breakdown
records which one.

### 2. Swedish independent label

The label credit is split into its component imprints first, so
`"Rama Lama Records / Sony Music Entertainment Sweden AB"` is read as an indie
release with major distribution rather than a major release.

| Signal | Weight | Source |
|---|---|---|
| Imprint is on the curated Swedish independent list | +0.5 … +0.95 | reference |
| MusicBrainz says the label is registered in Sweden | +0.75 | MusicBrainz |
| ≥ half the ISRCs have the `SE` country prefix | +0.45 | Spotify |
| Self-released and the artist is Swedish | +0.50 | derived |
| Label name is a Swedish company form (AB/HB/KB), uses å/ä/ö, or names a Swedish place | +0.30 | heuristic |
| Credited to a major (Universal / Sony / Warner) | −0.35 … −0.95 | reference |
| Known non-Swedish label with a Nordic-sounding name | −0.80 | reference |
| Credit is a DIY distributor (DistroKid, TuneCore, …) | −0.35 | reference |

**Heuristics alone never produce a match.** A release only matches this
criterion if an actual label was *named* — either from the curated list or
confirmed as Sweden-registered by MusicBrainz — with self-releases by confirmed
Swedish artists as the one clearly-labelled exception.

### 3. Swedish songwriters

Spotify publishes no songwriter credits, so this runs entirely through
MusicBrainz: `ISRC → recording → work → composer/lyricist/writer relations →
each writer's country`. Arrangers and translators do not count as writers.

A song qualifies when the Swedish share of its *country-known* writers reaches
`--writer-majority` (default 0.5); the release matches when at least that share
of its credited songs qualify.

**Missing data is reported, never guessed.** Writers whose nationality
MusicBrainz does not know are excluded from the ratio rather than assumed, and a
release with no resolvable credits reports "MusicBrainz has no songwriter
credits for this release yet" instead of matching or silently failing.

---

## Where the candidates come from

| Source | Used when | Why |
|---|---|---|
| `/browse/new-releases` per market | always | Spotify's own new-release feed |
| `search?q=label:"<name>" tag:new` for each curated label | `label` requested | Highest-precision source for criterion 2 |
| `search?q=tag:new year:<yyyy>` | `artist`/`writer` requested | Broad sweep beyond the Swedish carousel |
| `search?q=genre:"swedish …"` → each artist's recent albums | `artist`/`writer` requested | Catches Swedish artists the feeds miss |

---

## Rate limits, budgets and caching

- **Spotify** — batched wherever possible (20 albums, 50 artists, 50 tracks per
  request), with `Retry-After`-aware backoff on 429s.
- **MusicBrainz** — one request per second, as its guidelines require, with the
  contact address in the User-Agent. This is the slow part, so a run has a
  request budget (`--musicbrainz-budget`, default 600) and a per-release
  songwriter budget (`--writer-release-budget`, default 60). Anything not
  checked says so rather than reporting a false negative.
- **Cache** — SQLite, keyed by URL. Albums/artists 7 days, MusicBrainz 30 days,
  browse/search 1 hour. First runs are slow; later runs are fast.

---

## Maintaining the label list

`swedish_radar/data/swedish_labels.json` ships ~76 Swedish independents, plus
two lists that exist to *prevent* wrong matches: foreign labels with
Nordic-sounding names (Svart Records is Finnish, Fika Recordings is British),
and Swedish labels that are no longer independent.

```bash
swedish-radar labels check "Rama Lama Records" "Svart Records"
swedish-radar labels add "Nytt Skivbolag" --city Göteborg --scene "indie" --confidence 0.85
```

The `confidence` field is honest about uncertainty: entries whose current
ownership is unclear score lower and are annotated.

---

## Web UI

`swedish-radar serve` starts a local page bound to `127.0.0.1`. It has no
authentication and holds your Spotify credentials in the serving process — keep
it on localhost.

Runs happen in a background thread with a live progress log; results are stored
in SQLite so previous runs stay browsable.

---

## Automation

Two workflows ship with the repository:

- `.github/workflows/swedish-radar-tests.yml` runs the suite on Python 3.9,
  3.11 and 3.13. Nothing to install — there are no dependencies.
- `.github/workflows/swedish-radar-digest.yml` runs a weekly pass and commits
  `radar/latest.md` and `radar/latest.json`. It needs the `SPOTIFY_CLIENT_ID`
  and `SPOTIFY_CLIENT_SECRET` repository secrets; without them it reports that
  and exits successfully rather than failing. The response cache is carried
  between runs, which is what keeps the MusicBrainz stage to minutes.

---

## Honest limitations

- **Songwriter coverage is partial.** MusicBrainz work-level credits are
  excellent for established repertoire and sparse for brand-new releases. The
  app tells you the coverage rather than papering over it.
- **The label list is curated, so it is incomplete.** MusicBrainz's label
  country fills some gaps; the rest is a `labels add` away.
- **Spotify's `label` field is a text credit**, not an identifier. It may hold a
  distributor, an artist name, or a major's subsidiary.
- **Spotify genre tags are algorithmic**, so `swedish pop` is good evidence but
  not proof.
- **Dual nationality and diaspora artists** get one country in MusicBrainz. The
  evidence panel shows what it used, so you can disagree with it.
