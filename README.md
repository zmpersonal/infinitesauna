# Infinite Sauna — homepage and compare UX patch

This patch fixes the split between the static homepage card list and the live
`data/saunas.json` used by the comparison engine.

## Main changes

- Homepage cards now render from the live JSON database.
- Brand/search/filter controls therefore work for newly added external models.
- External-only seed records are retained if an outside catalog is temporarily unavailable.
- Homepage product order is retailer-diversified and retailer names are visible on cards.
- New retailer coverage section on the homepage.
- Compare workflow is Brand -> searchable model/SKU.
- Comparison table uses fixed/equal model columns with wrapping for long SKUs.
- InHouse-carried comparison models link directly to their InHouse Wellness listing.

See `UPLOAD_INSTRUCTIONS.txt` for deployment steps.
