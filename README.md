# riksbank
Proxy för Riksbankens valutakurser
Uppdatering för Pages rebuild

---

# Swedish Radar

Also in this repository: **Swedish Radar**, an app that finds new Spotify
releases with a Swedish connection — by Swedish artists, on Swedish independent
record labels, or written primarily by Swedish songwriters. Pick any combination
of the three criteria; every match shows the evidence behind it.

```bash
cp .env.example .env          # add your Spotify Client ID and Secret
python3 -m swedish_radar doctor
python3 -m swedish_radar discover --criteria artist,label,writer --days 14
python3 -m swedish_radar serve # web UI on http://127.0.0.1:8000
```

No runtime dependencies — Python 3.9+ standard library only.

- [Full documentation](swedish_radar/README.md)
- [How each criterion is decided](docs/methodology.md)
- Daily digests (when the workflow is enabled): [`radar/latest.md`](radar/latest.md)

