# Infinite Sauna

Infinite Sauna is a static, source-documented sauna specification database. A weekly GitHub Actions workflow refreshes the catalog and regenerates the public research pages.

## Main resources

- `/database/` — complete searchable model database
- `/electrical/` — documented voltage, amperage, plug and heater requirements
- `/data/` — downloadable CSV and JSON with Dataset structured data
- `/changes/` — current release and coverage metrics
- `/brands/` and model pages — normalized entity records
- data-driven category pages for 120V, 240V, infrared, traditional, outdoor and capacity groups
- `/methodology/` — source hierarchy, matching, taxonomy and limitations

## Updating

Run `python scripts/update_database.py` for a live refresh or `python scripts/update_database.py --seed-only` to regenerate pages from the committed dataset without network retrieval.
