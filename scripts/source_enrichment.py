#!/usr/bin/env python3
"""Retailer/source enrichment for InfiniteSauna.

This module is intentionally separate from update_database.py so the core InHouse
catalog refresh stays small and stable. It adds:
- deterministic, source-backed specification overrides from manufacturer_sources.csv
- current retailer offers from direct product URLs
- scalable Shopify collection ingestion from retailer_catalogs.csv
- optional new models from retailers that sell brands InHouse does not carry
"""
import csv
import json
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

SPEC_FIELDS = [
    'capacity','type','placement','emf','spectrum','red_light','voltage','amperage','plug',
    'ir_wattage','heater','heater_kw','max_temp','exterior_dimensions','interior_dimensions',
    'wood','weight','warranty'
]


def clean(v):
    if v is None:
        return None
    v = re.sub(r'\s+', ' ', str(v)).strip(' :-\t\r\n')
    return v or None


def money(v):
    try:
        return float(str(v).replace(',', '').replace('$', '').strip())
    except Exception:
        return None


def slug(s):
    s = (s or '').lower().replace('‑','-').replace('–','-').replace('—','-')
    return re.sub(r'[^a-z0-9]+', '-', s).strip('-')[:100] or 'sauna'


def norm_id(v):
    return re.sub(r'[^A-Z0-9]+', '', (v or '').upper())


def csv_bool(v, default=False):
    if v in (None, ''):
        return default
    return str(v).strip().lower() not in ('0','false','no','off')


def load_rows(path):
    if not path.exists():
        return []
    with path.open(newline='') as f:
        return [r for r in csv.DictReader(f) if csv_bool(r.get('active', '1'), True)]


def same_model(a, b):
    a, b = norm_id(a), norm_id(b)
    if not a or not b:
        return False
    if a == b:
        return True
    # Common material / edition suffixes vary by retailer SKU conventions.
    suffixes = ('ELITE', 'CED', 'HEM', 'FS', 'ZF')
    for suffix in suffixes:
        if a == b + suffix or b == a + suffix:
            return True
    return False


def find_target(products, model_key=None, match_text=None, model=None, title=None, infer_model=None):
    if model_key:
        key = slug(model_key)
        hit = next((p for p in products if p.get('model_key') == key), None)
        if hit:
            return hit
    if model:
        wanted = norm_id(model)
        # Prefer an exact normalized model/SKU match before allowing known suffix equivalence.
        hit = next((p for p in products if wanted and (norm_id(p.get('model')) == wanted or norm_id(p.get('model_key')) == wanted)), None)
        if hit:
            return hit
        hit = next((p for p in products if same_model(p.get('model'), model) or same_model(p.get('model_key'), model)), None)
        if hit:
            return hit
    if match_text:
        needle = norm_id(match_text)
        hits = [p for p in products if needle and needle in norm_id((p.get('model') or '') + ' ' + (p.get('title') or ''))]
        if len(hits) == 1:
            return hits[0]
    if title and infer_model:
        inferred = infer_model(title)
        if inferred:
            return next((p for p in products if same_model(p.get('model'), inferred)), None)
    return None


def add_offer(product, retailer, url, price=None, reference_price=None, source_type='retailer'):
    retailer, url = clean(retailer), clean(url)
    if not retailer or not url:
        return
    offers = product.setdefault('retailer_offers', [])
    hit = next((o for o in offers if (o.get('retailer') or '').lower() == retailer.lower()), None)
    payload = {
        'retailer': retailer,
        'url': url,
        'price': price,
        'reference_price': reference_price,
        'source_type': source_type,
        'checked_at': datetime.now(timezone.utc).date().isoformat(),
    }
    if hit:
        hit.update({k:v for k,v in payload.items() if v not in (None, '')})
    else:
        offers.append(payload)


def ensure_inhouse_offer(products):
    for p in products:
        if p.get('inhouse_url'):
            add_offer(p, 'InHouse Wellness', p['inhouse_url'], p.get('price'), p.get('reference_price'), 'featured')


def apply_verified_overrides(products, manufacturer_csv, infer_model=None):
    """Apply explicit values from source rows without overwriting populated values.

    This is useful when a manufacturer page contains several configurations in one
    document and generic HTML parsing could attach the wrong size/heater to a model.
    """
    for row in load_rows(manufacturer_csv):
        target = find_target(products, row.get('model_key'), row.get('match_text'), row.get('model'), infer_model=infer_model)
        if not target:
            continue
        for field in SPEC_FIELDS:
            raw = clean(row.get(field))
            if raw is None or target.get(field) not in (None, ''):
                continue
            if field == 'capacity':
                try:
                    raw = int(float(raw))
                except Exception:
                    pass
            elif field == 'red_light':
                raw = csv_bool(raw)
            target[field] = raw
        url = clean(row.get('url'))
        if url:
            urls = target.setdefault('source_urls', [])
            if url not in urls:
                urls.append(url)
    return products


def _json_nodes(obj):
    queue = obj if isinstance(obj, list) else [obj]
    while queue:
        node = queue.pop(0)
        if isinstance(node, list):
            queue.extend(node)
        elif isinstance(node, dict):
            yield node
            if isinstance(node.get('@graph'), list):
                queue.extend(node['@graph'])


def _json_offer_prices(offers):
    offers = offers if isinstance(offers, list) else [offers]
    current, reference = [], []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        for key in ('price', 'lowPrice'):
            v = money(offer.get(key))
            if v:
                current.append(v)
        for key in ('highPrice', 'listPrice', 'msrp'):
            v = money(offer.get(key))
            if v:
                reference.append(v)
    return (min(current) if current else None, max(reference) if reference else None)


def fetch_product_page(url, sess, BeautifulSoup, parse_specs, infer_model):
    r = sess.get(url, timeout=40)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, 'html.parser')
    text = soup.get_text('\n', strip=True)
    out = {'url': url, 'specs': parse_specs(text)}
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            obj = json.loads(tag.get_text(strip=True) or '{}')
        except Exception:
            continue
        product = None
        for node in _json_nodes(obj):
            typ = node.get('@type')
            types = typ if isinstance(typ, list) else [typ]
            if 'Product' in types:
                product = node
                break
        if not product:
            continue
        out['title'] = clean(product.get('name'))
        out['model'] = clean(product.get('sku') or product.get('mpn') or product.get('model')) or infer_model(out.get('title'))
        brand = product.get('brand')
        if isinstance(brand, dict):
            brand = brand.get('name')
        out['brand'] = clean(brand)
        image = product.get('image')
        if isinstance(image, list):
            image = image[0] if image else None
        if isinstance(image, dict):
            image = image.get('url')
        out['image'] = clean(image)
        out['price'], out['reference_price'] = _json_offer_prices(product.get('offers'))
        break
    if not out.get('price'):
        for attrs in ({'property':'product:price:amount'}, {'property':'og:price:amount'}):
            tag = soup.find('meta', attrs=attrs)
            if tag and money(tag.get('content')):
                out['price'] = money(tag.get('content'))
                break
    if not out.get('title') and soup.title:
        out['title'] = clean(soup.title.get_text(' ', strip=True))
    if not out.get('model'):
        out['model'] = infer_model((out.get('title') or '') + '\n' + text[:7000])
    if not out.get('image'):
        tag = soup.find('meta', attrs={'property':'og:image'})
        if tag:
            out['image'] = clean(tag.get('content'))
    return out


def shopify_candidate(raw, base_url, retailer, BeautifulSoup, parse_specs, infer_model):
    variants = raw.get('variants') or []
    active = [v for v in variants if v.get('available', True)] or variants
    choices = [(money(v.get('price')), v) for v in active if money(v.get('price'))]
    if not choices:
        return None
    price, chosen = min(choices, key=lambda x: x[0])
    refs = [money(v.get('compare_at_price')) for v in variants if money(v.get('compare_at_price'))]
    title = clean(raw.get('title')) or 'Sauna'
    handle = clean(raw.get('handle')) or slug(title)
    sku = clean(chosen.get('sku')) or infer_model(title)
    body_html = raw.get('body_html') or ''
    body = BeautifulSoup(body_html, 'html.parser').get_text('\n', strip=True)
    images = raw.get('images') or []
    return {
        'retailer': retailer,
        'brand': clean(raw.get('vendor')) or retailer,
        'model': sku,
        'title': title,
        'price': price,
        'reference_price': max(refs) if refs else None,
        'url': f"{base_url.rstrip('/')}/products/{handle}",
        'image': images[0].get('src') if images else None,
        'specs': parse_specs('\n'.join([title, str(raw.get('tags') or ''), body])),
    }


def fetch_shopify_catalog(base_url, collection_path, retailer, sess, BeautifulSoup, parse_specs, infer_model, max_pages=6):
    path = '/' + (collection_path or 'collections/all').strip('/')
    if not path.startswith('/collections/'):
        path = '/collections/' + path.strip('/')
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        url = f"{base_url.rstrip('/')}{path}/products.json?limit=250&page={page}"
        r = sess.get(url, timeout=45)
        r.raise_for_status()
        items = r.json().get('products', [])
        if not items:
            break
        for raw in items:
            pid = raw.get('id')
            if pid in seen:
                continue
            seen.add(pid)
            c = shopify_candidate(raw, base_url, retailer, BeautifulSoup, parse_specs, infer_model)
            if c:
                out.append(c)
        if len(items) < 250:
            break
    return out


def make_external_product(cand):
    model = clean(cand.get('model')) or slug(cand.get('title'))
    p = {
        'model_key': slug(model),
        'brand': clean(cand.get('brand')) or clean(cand.get('retailer')) or 'Unknown',
        'model': model,
        'title': clean(cand.get('title')) or model,
        'price': cand.get('price'),
        'reference_price': cand.get('reference_price'),
        'inhouse_url': None,
        'image': cand.get('image'),
        'source_urls': [cand.get('url')] if cand.get('url') else [],
        **(cand.get('specs') or {}),
    }
    add_offer(p, cand.get('retailer'), cand.get('url'), cand.get('price'), cand.get('reference_price'))
    return p


def enrich_retailers(products, root, sess, BeautifulSoup, parse_specs, infer_model):
    ensure_inhouse_offer(products)
    direct_csv = root / 'data' / 'retailer_sources.csv'
    catalog_csv = root / 'data' / 'retailer_catalogs.csv'

    for row in load_rows(direct_csv):
        retailer, url = clean(row.get('retailer')), clean(row.get('url'))
        if not retailer or not url:
            continue
        target = find_target(products, row.get('model_key'), row.get('match_text'), row.get('model'), row.get('title'), infer_model)
        try:
            page = fetch_product_page(url, sess, BeautifulSoup, parse_specs, infer_model)
        except Exception as e:
            print(f'Retail source skipped {retailer}: {e}', file=sys.stderr)
            continue
        if target is None and csv_bool(row.get('allow_new'), False):
            cand = {
                **page,
                'retailer': retailer,
                'url': url,
                'brand': clean(row.get('brand')) or page.get('brand'),
                'model': clean(row.get('model')) or page.get('model'),
                'title': clean(row.get('title')) or page.get('title'),
                'specs': page.get('specs') or {},
            }
            target = make_external_product(cand)
            products.append(target)
        if target:
            add_offer(target, retailer, url, page.get('price'), page.get('reference_price'))
            urls = target.setdefault('source_urls', [])
            if url not in urls:
                urls.append(url)
            if csv_bool(row.get('use_specs'), False):
                for field, value in (page.get('specs') or {}).items():
                    if field in SPEC_FIELDS and not target.get(field):
                        target[field] = value
                if page.get('image') and not target.get('image'):
                    target['image'] = page['image']
        time.sleep(.15)

    for row in load_rows(catalog_csv):
        retailer = clean(row.get('retailer'))
        base_url = clean(row.get('base_url'))
        collection = clean(row.get('collection_path'))
        if not retailer or not base_url:
            continue
        try:
            candidates = fetch_shopify_catalog(
                base_url, collection, retailer, sess, BeautifulSoup, parse_specs, infer_model,
                int(row.get('max_pages') or 6)
            )
        except Exception as e:
            print(f'Retail catalog skipped {retailer}: {e}', file=sys.stderr)
            continue
        title_re, brand_re = clean(row.get('title_regex')), clean(row.get('brand_regex'))
        for cand in candidates:
            if title_re and not re.search(title_re, cand.get('title') or '', re.I):
                continue
            if brand_re and not re.search(brand_re, cand.get('brand') or '', re.I):
                continue
            target = find_target(products, model=cand.get('model'), title=cand.get('title'), infer_model=infer_model)
            if target is None and csv_bool(row.get('allow_new'), False):
                target = make_external_product(cand)
                products.append(target)
            if not target:
                continue
            add_offer(
                target, retailer, cand['url'], cand.get('price'), cand.get('reference_price'),
                'manufacturer-direct' if csv_bool(row.get('manufacturer_direct'), False) else 'retailer'
            )
            urls = target.setdefault('source_urls', [])
            if cand['url'] not in urls:
                urls.append(cand['url'])
            if csv_bool(row.get('use_specs'), False):
                for field, value in (cand.get('specs') or {}).items():
                    if field in SPEC_FIELDS and not target.get(field):
                        target[field] = value
                if cand.get('image') and not target.get('image'):
                    target['image'] = cand['image']
        print(f'Checked {len(candidates)} products at {retailer}')
        time.sleep(.25)

    # InHouse remains first where it sells the model; external-only models use the
    # lowest currently captured offer as their display price.
    for p in products:
        offers = p.get('retailer_offers') or []
        offers.sort(key=lambda o: (0 if (o.get('retailer') or '').lower() == 'inhouse wellness' else 1, (o.get('retailer') or '').lower()))
        if not p.get('inhouse_url') and offers:
            priced = [o for o in offers if isinstance(o.get('price'), (int, float))]
            chosen = min(priced, key=lambda o: o['price']) if priced else offers[0]
            p['price'] = chosen.get('price')
            p['reference_price'] = chosen.get('reference_price')
    return products
