# InfiniteSauna.com

A static, GitHub Pages-ready sauna model database and comparison engine.

## What it does

- Searches and filters sauna models by brand, type, indoor/outdoor placement, capacity, voltage and EMF terminology.
- Lets visitors compare up to four models in one table.
- Generates an SEO page for every model and brand.
- Generates a small set of curated `model-vs-model` comparison pages from `data/comparisons.csv`.
- Features the matching InHouse Wellness listing as the primary retail link.
- Refreshes the InHouse Wellness sauna catalog weekly using a GitHub Action.
- Parses product-page text for technical fields such as voltage, amperage, plug, dimensions, heater information, temperature, wood/materials and warranty.
- Can fill missing fields from official manufacturer URLs in `data/manufacturer_sources.csv`.
- Exports both JSON and CSV.

## First GitHub setup

1. Create a GitHub repository and upload **all** files and folders in this package, including the hidden `.github` folder.
2. Go to **Settings → Pages** and set **Source** to **GitHub Actions**.
3. Go to the repository's **Actions** tab.
4. Open **Update sauna database and deploy**.
5. Click **Run workflow** and run it on `main` once.
6. Set the custom domain in **Settings → Pages** to `infinitesauna.com`.
7. Point the domain DNS to GitHub Pages as you did for the other sites.

No API key is required for the current updater.

## Automatic refresh

`.github/workflows/update-data.yml` runs every Monday and can also be run manually. It:

1. reads the live InHouse Wellness sauna collection,
2. normalizes catalog information,
3. extracts available technical specifications,
4. checks configured manufacturer enrichment sources,
5. rewrites `data/saunas.json` and `data/saunas.csv`,
6. regenerates model, brand and curated comparison pages,
7. updates the sitemap,
8. commits changed data, and
9. deploys the refreshed site.

## Adding manufacturer sources

Edit `data/manufacturer_sources.csv`. `match_text` can be used when a retailer SKU creates a different model key. `fetch_page=1` enables page parsing; `fetch_page=0` registers the source URL and uses only explicit verified values in that row. Manufacturer/specification rows fill missing fields and are separate from competing retail offers.

## Adding curated comparisons

Edit `data/comparisons.csv`:

```csv
model_a,model_b,reason
fd-4,fd-5,Adjacent Finnmark hybrid models
```

Only curated pairs are generated as indexable static pages. The main `/compare/` tool can compare any models interactively.

## Important data rule

The scraper intentionally leaves uncertain fields as `Not verified`. Do not change it to infer electrical or safety-critical values from adjacent models.

## Multi-source specification and retailer enrichment

The updater now has three source registries:

- `data/manufacturer_sources.csv` — controlled technical-specification sources and explicit verified overrides.
- `data/retailer_sources.csv` — direct exact-model retailer price checks.
- `data/retailer_catalogs.csv` — scalable Shopify-style catalog sources for offer matching and optional external-only model discovery.

`InHouse Wellness` remains the featured offer whenever a model has an InHouse URL. Other captured offers are stored in each product's `retailer_offers` array and shown in the live comparison table. External-only models can be added only by catalog/source rows with `allow_new=1`.

### Adding a technical source

Add a row to `manufacturer_sources.csv`. Use `fetch_page=0` plus explicit columns for a source page that contains several configurations/heaters and could be mis-parsed. Use `fetch_page=1` when the page is model-specific enough for automated text extraction. Explicit values only fill blank fields.

### Adding a retailer

For one important model, add its product page to `retailer_sources.csv`. For a Shopify-compatible collection, add a row to `retailer_catalogs.csv`. Set `allow_new=0` when the catalog should only match existing models; set it to `1` only when you intentionally want that source to expand the catalog.

### Safety/quality rules

- Exact normalized model/SKU matches are preferred before suffix matching.
- Existing populated technical fields are preserved.
- InHouse offers are sorted first where available.
- A missing field remains `Not verified`.
- Retail prices are snapshots; heater/configuration equivalence must still be checked.

