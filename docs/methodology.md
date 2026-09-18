# Methodology

How Swedish Radar turns Spotify and MusicBrainz data into a yes/no verdict per
criterion, and why it is built to be argued with rather than believed.

## The shape of a verdict

Each criterion produces a `Verdict`:

```json
{
  "criterion": "label",
  "matched": true,
  "confidence": 0.97,
  "summary": "Released on Adrian Recordings (Swedish independent, Malmö) - indie, electronic.",
  "signals": [
    {"code": "curated_label", "detail": "…", "weight": 0.95, "source": "reference"},
    {"code": "isrc_se",       "detail": "…", "weight": 0.45, "source": "spotify"}
  ],
  "data": { "...": "criterion-specific detail" }
}
```

A signal's `weight` is the probability that signal alone would assign to the
claim. Negative weights argue against it.

## Combining signals

Signals combine with **noisy-OR**, then negatives discount the result:

```
score = 1 - Π(1 - wᵢ)        for every positive wᵢ
score = score · Π(1 - |wⱼ|)  for every negative wⱼ
```

Two 0.4 signals give 0.64 — mutually reinforcing, but still short of the 0.55
default threshold on their own merits only when a single one fires. One 0.9
signal with a −0.5 counter-signal gives 0.45, which falls below threshold.

This shape was chosen for three properties:

1. **No single weak signal can carry a match.** The strongest heuristic
   (`title_language`) maxes out at 0.45, below the 0.55 default threshold.
2. **Independent weak evidence accumulates.** Three modest hints outweigh one.
3. **Counter-evidence is multiplicative**, so a MusicBrainz country of `US`
   (−0.90) collapses a score regardless of how many hints point the other way.

## Criterion 1 — Swedish artist

**Primary source: MusicBrainz.** Resolution runs in two steps, and *how* an
artist was resolved changes the weight:

1. `GET /ws/2/url?resource=https://open.spotify.com/artist/<id>&inc=artist-rels`
   gives an exact Spotify→MusicBrainz mapping. Country from this path: **+0.95**.
2. Failing that, a name search that must return exactly **one** hit with an
   exact name match and score ≥ 92. Weaker, so: **+0.75**. Ambiguous results are
   discarded rather than guessed — a wrong artist would poison the verdict.

Country is read from `country`, then `area`, then `begin-area`.

**Supporting signals** (Spotify): genre tags, ISRC country prefixes, Sweden-only
market availability, Swedish-language titles, and being on a Swedish indie label.

**Multi-artist releases** are scored per artist; the release takes the best
artist's score and records the full breakdown, so a Swedish feature on a foreign
record still surfaces, labelled correctly.

## Criterion 2 — Swedish independent label

### Splitting the credit

Spotify's `album.label` is free text. It is first split on `/`, `|`, `;`, dashes,
and on phrases like `under exclusive license to`, `distributed by`,
`a division of`. The ℗/© copyright lines are parsed too — they often name the
real imprint when `label` holds a distributor.

### Matching

Each imprint is normalised (accents folded, punctuation dropped, corporate
suffixes like `AB`/`Ltd`/`GmbH` removed) and looked up two ways:

- **Exact key**: `"Adrian Recordings AB"` → `adrian recordings`.
- **Loose key**: also drops industry words, so `"Adrian Records"` → `adrian`
  still finds `Adrian Recordings` at 80% of its weight.

A loose key shorter than three characters, or one that collapses to nothing
(`"Music AB"` → `""`), never matches — otherwise every generic name would match
every other.

### Independence

Three lists do the rejecting:

- **Majors** — ~61 markers across Universal, Sony and Warner, including their
  Swedish arms (`Universal Music AB`, `Stockholm Records`, `Sonet`) and
  distribution services (`The Orchard`, `ADA`, `Spinnup`, `AWAL`).
- **False friends** — non-Swedish labels whose names read as Nordic. Svart
  Records is Finnish; Fika Recordings is British; Fysisk Format and Smalltown
  Supersound are Norwegian; Posh Isolation is Danish. Without this list the
  language heuristics would misfire constantly.
- **Aggregators** — DistroKid, TuneCore, CD Baby and friends. Seeing one means
  the label field carries *no label information*, which is different from
  carrying bad information.

A curated indie credited **alongside** a major is discounted (−0.35), not
rejected: that is a licensing or distribution deal, and the release is still
that indie's. A bare major credit is fatal (−0.95).

### The naming rule

Heuristics (Swedish company form, å/ä/ö, Swedish place names, SE ISRCs) can
*support* a match but never create one. A release matches only when a label was
actually named:

- it is on the curated list, **or**
- MusicBrainz resolved the release's barcode to a Sweden-registered label, **or**
- it is self-released by a confirmed Swedish artist (said so in the summary).

This is the single most important guard against false positives, because
"sounds Swedish" is not "is a Swedish independent label".

## Criterion 3 — Swedish songwriters

Spotify exposes no songwriting credits at any tier of its API, so this criterion
is MusicBrainz end to end:

```
ISRC  →  /ws/2/isrc/<isrc>?inc=work-rels   →  work MBIDs
work  →  /ws/2/work/<mbid>?inc=artist-rels →  composer / lyricist / writer / librettist
writer → /ws/2/artist/<mbid>               →  country
```

`arranger`, `orchestrator` and `translator` relations are deliberately excluded:
they are not writing credits.

### "Primarily"

Per song: `swedish_share = swedish_writers / writers_whose_country_is_known`.
A song qualifies at `swedish_share >= writer_majority` (default 0.5).
The release qualifies when that same share of its *credited* songs qualify.

Writers of unknown nationality are **excluded from the denominator**, never
counted as non-Swedish (which would create false negatives) and never assumed
Swedish (which would create false positives).

### Coverage is reported, not hidden

Confidence is scaled by two coverage factors:

- **Completeness** — how much of each qualifying work's writer list has a known
  country. 1 of 5 writers identified is weaker evidence than 4 of 4.
- **Coverage** — how many of the sampled songs resolved at all. Below 50%, a
  penalty of up to −0.30 applies.

A release with no resolvable credits produces `matched: false` with an explicit
summary, and a release skipped because the budget ran out says *that* instead —
"not checked" and "checked and found nothing" are different answers.

## Budgets

MusicBrainz asks anonymous clients for ≤ 1 request/second. A full run over 400
releases would otherwise take hours, so:

- `--musicbrainz-budget` (default 600) caps total MusicBrainz requests.
- `--writer-release-budget` (default 60) caps how many releases spend budget on
  songwriter lookups.
- `--writer-track-sample` (default 4) caps tracks sampled per release.

Everything is cached for 30 days, so the budget mostly bites on first runs.
When a budget is exhausted the run says so in `stats.errors` and the affected
verdicts say so in their summaries.

## Ranking

```
score = best_matching_confidence + 0.08 × (extra_criteria_matched) + freshness
```

where `freshness` decays linearly from +0.06 at release day to 0 at 30 days old.
Matching two criteria at 0.8 outranks matching one at 0.85, which is the
behaviour you want from a discovery tool. The score is a ranking number and can
exceed 1.0 — the per-criterion confidences it is built from cannot.

## Known failure modes

| Situation | What happens | Why it is acceptable |
|---|---|---|
| Brand-new release, no MusicBrainz entry | Artist criterion falls back to Spotify genre/ISRC/language signals; writer criterion reports no credits | Honest degradation; the evidence panel shows exactly which sources fired |
| Swedish label not on the curated list, no MusicBrainz barcode match | Missed | `labels add` fixes it permanently; heuristics still surface it under the artist criterion if the artist is Swedish |
| Swedish-born artist registered elsewhere in MusicBrainz | Missed or downweighted | MusicBrainz is the best available authority; the verdict shows the country it used |
| Compilation with many artists | Scored on its strongest artist | Discovery tools should surface, not suppress |
| Spotify `label` holds the artist name | Treated as self-released; matches only if the artist is confirmed Swedish | Avoids treating every DIY release as a label |
