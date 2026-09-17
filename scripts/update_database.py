#!/usr/bin/env python3
import csv
import html
import json
import re
import sys
import time
import random
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from source_enrichment import apply_verified_overrides, enrich_retailers

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data' / 'saunas.json'
CSV_OUT = ROOT / 'data' / 'saunas.csv'
MANUFACTURERS = ROOT / 'data' / 'manufacturer_sources.csv'
COMPARISONS = ROOT / 'data' / 'comparisons.csv'
RETAILERS = ROOT / 'data' / 'retailers.csv'
BASE = 'https://inhousewellness.com'
UA = 'InfiniteSaunaDatabaseBot/1.0 (+https://infinitesauna.com/methodology/)'

try:
    import requests
    from bs4 import BeautifulSoup
except Exception:
    requests = None
    BeautifulSoup = None


def slug(s):
    s = (s or '').lower().replace('‑','-').replace('–','-').replace('—','-')
    return re.sub(r'[^a-z0-9]+', '-', s).strip('-')[:100] or 'sauna'


def clean(v):
    if v is None:
        return None
    v = re.sub(r'\s+', ' ', str(v)).strip(' :-\t\r\n')
    return v or None


def money(v):
    try:
        return float(str(v).replace(',', '').replace('$',''))
    except Exception:
        return None


def infer_capacity(text):
    t = (text or '').lower().replace('–','-')
    pats = [r'up to\s*(\d+)\s*(?:people|persons|bathers)', r'(\d+)\s*(?:-|to\s*)?(\d+)?\s*(?:person|people|bather)']
    for p in pats:
        m = re.search(p, t)
        if m:
            nums = [int(x) for x in m.groups() if x]
            return max(nums) if nums else None
    return None


def infer_type(text):
    t = (text or '').lower()
    has_ir = any(k in t for k in ['infrared', 'far ir', 'full spectrum', 'near infrared', 'low emf', 'near zero emf'])
    has_trad = any(k in t for k in ['traditional', 'steam sauna', 'rock heater', 'sauna heater', 'harvia', 'huum', 'wood burning'])
    # A retailer page can mention a traditional heater in navigation or related
    # products. Only call a model hybrid when the product itself says so.
    explicit_hybrid = any(k in t for k in ['hybrid sauna', 'infrared + traditional', 'infrared and traditional', 'dual heat system'])
    if explicit_hybrid and has_ir and has_trad:
        return 'Hybrid'
    if has_ir:
        return 'Infrared'
    return 'Traditional'


def infer_placement(text):
    t = (text or '').lower()
    return 'Outdoor' if any(k in t for k in ['outdoor', 'barrel', 'garden-series', 'cube-series', 'pod sauna']) else 'Indoor'


def infer_emf(text):
    t = (text or '').lower().replace('‑','-')
    if 'near zero emf' in t or 'near-zero emf' in t:
        return 'Near Zero'
    if 'ultra low emf' in t or 'ultra-low emf' in t:
        return 'Ultra Low'
    if 'low emf' in t or 'low-emf' in t:
        return 'Low'
    return None


def infer_spectrum(text):
    t = (text or '').lower().replace('‑','-')
    if 'full spectrum' in t or 'full-spectrum' in t:
        return 'Full Spectrum'
    if 'far infrared' in t or 'far ir' in t:
        return 'Far Infrared'
    if 'infrared' in t:
        return 'Infrared'
    return None


def find_labeled_line(lines, labels):
    for line in lines:
        low = line.lower()
        for lab in labels:
            idx = low.find(lab.lower())
            if idx >= 0:
                val = line[idx + len(lab):]
                val = clean(val)
                if val:
                    return val
    return None


def normalize_dims(v):
    if not v:
        return None
    v = v.replace('”','"').replace('“','"').replace('′',"'").replace('×','x')
    m = re.search(r'(\d+(?:\.\d+)?\s*(?:"|in(?:ches)?)?\s*[xX]\s*\d+(?:\.\d+)?\s*(?:"|in(?:ches)?)?\s*[xX]\s*\d+(?:\.\d+)?\s*(?:"|in(?:ches)?)?)', v, re.I)
    if m:
        return clean(m.group(1).replace(' X ', ' x ').replace('X','x'))
    m = re.search(r'(\d+(?:\.\d+)?\s*(?:"|in(?:ches)?)?\s*(?:diameter|dia\.?)\s*(?:&|x|by)\s*\d+(?:\.\d+)?\s*(?:"|in(?:ches)?)?\s*(?:length|long)?)', v, re.I)
    return clean(m.group(1)) if m else clean(v[:90])


def parse_specs(text):
    text = (text or '').replace('\xa0', ' ')
    lines = [clean(x) for x in re.split(r'[\r\n]+', text) if clean(x)]
    low = text.lower().replace('‑','-').replace('–','-')
    specs = {}

    specs['capacity'] = infer_capacity(text)
    specs['type'] = infer_type(text)
    specs['placement'] = infer_placement(text)
    specs['emf'] = infer_emf(text)
    specs['spectrum'] = infer_spectrum(text)
    specs['red_light'] = True if 'red light' in low else None

    voltages = sorted(set(re.findall(r'\b(120|208|220|230|240)\s*v\b', low)))
    if voltages:
        specs['voltage'] = '/'.join(v + 'V' for v in voltages)

    amp = re.search(r'\b(\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)?)\s*(?:amps?|a)\b', low)
    if amp:
        specs['amperage'] = amp.group(1).replace(' ', '') + 'A'

    plug = re.search(r'\bNEMA\s*[0-9]+-[0-9]+[PR]?\b', text, re.I)
    if plug:
        specs['plug'] = plug.group(0).upper().replace(' ', '')
    elif 'standard 120v outlet' in low or 'standard wall outlet' in low or 'plug & play' in low or 'plug and play' in low:
        specs['plug'] = 'Standard 120V outlet'

    irw = re.search(r'(?:IR\s*)?Wattage\s*:?\s*([0-9,.]+)\s*W', text, re.I)
    if irw:
        specs['ir_wattage'] = clean(irw.group(1).replace(',','') + 'W')

    heater = find_labeled_line(lines, ['Traditional Heater:', 'Heater:', 'Sauna Heater:'])
    if heater:
        specs['heater'] = heater
    else:
        h = re.search(r'((?:Harvia|HUUM|Scandia|Narvi|Cozy Heat)[^\n\r.;]{0,80}(?:\d+(?:\.\d+)?\s*kW)?)', text, re.I)
        if h:
            specs['heater'] = clean(h.group(1))

    kw = re.search(r'\b(\d+(?:\.\d+)?)\s*kW\b', text, re.I)
    if kw:
        specs['heater_kw'] = kw.group(1) + ' kW'

    maxt = find_labeled_line(lines, ['Maximum Temperatures:', 'Maximum Temperature:', 'Max Temperature:', 'Temperature Range:'])
    if maxt:
        specs['max_temp'] = clean(maxt[:100])
    else:
        temps = [int(x) for x in re.findall(r'\b(1[4-9]\d|2[0-1]\d)\s*°?\s*F\b', text, re.I)]
        if temps:
            specs['max_temp'] = f"Up to {max(temps)}°F"

    ext = find_labeled_line(lines, ['Assembled Exterior Dimensions (WDH):', 'Exterior Dimensions:', 'Outside Dimensions:', 'Exterior dimension:'])
    if ext:
        specs['exterior_dimensions'] = normalize_dims(ext)
    interior = find_labeled_line(lines, ['Assembled Interior Dimensions (WDH):', 'Interior Dimensions:', 'Inside Dimensions:'])
    if interior:
        specs['interior_dimensions'] = normalize_dims(interior)
    if not specs.get('exterior_dimensions'):
        generic = find_labeled_line(lines, ['Dimensions:'])
        if generic:
            specs['exterior_dimensions'] = normalize_dims(generic)

    woods = []
    wood_terms = [
        ('Western Red Cedar', ['western red cedar']),
        ('Canadian Hemlock', ['canadian hemlock', 'hemlock']),
        ('Pacific Cedar', ['pacific cedar']),
        ('Thermo-Spruce', ['thermo-spruce', 'thermo spruce']),
        ('Thermo-Aspen', ['thermo-aspen', 'thermo aspen']),
        ('Aspen', ['aspen']),
        ('Cedar', ['cedar']),
        ('Spruce', ['spruce']),
    ]
    for label, terms in wood_terms:
        if any(term in low for term in terms):
            if not any(label in existing or existing in label for existing in woods):
                woods.append(label)
    if woods:
        specs['wood'] = ' / '.join(woods[:3])

    wt = find_labeled_line(lines, ['Product Weight:', 'Assembled Weight:', 'Weight:'])
    if wt:
        m = re.search(r'([0-9,.]+\s*lbs?)', wt, re.I)
        specs['weight'] = m.group(1) if m else clean(wt[:50])

    warranty = find_labeled_line(lines, ['Warranty:'])
    if warranty:
        specs['warranty'] = clean(warranty[:100])
    elif 'limited lifetime warranty' in low:
        specs['warranty'] = 'Limited lifetime warranty'
    elif '5-year' in low or '5 year' in low:
        specs['warranty'] = '5-year limited warranty'

    return {k:v for k,v in specs.items() if v not in (None, '', [])}


def session():
    s = requests.Session()
    s.headers.update({'User-Agent': UA, 'Accept': 'text/html,application/json'})
    return s


def load_existing():
    if DATA.exists():
        return json.loads(DATA.read_text())
    return {'products': []}


def get_shopify_products():
    s = session()
    all_products = []
    seen = set()
    for page in range(1, 8):
        url = f'{BASE}/collections/saunas/products.json?limit=250&page={page}'
        r = s.get(url, timeout=35)
        r.raise_for_status()
        items = r.json().get('products', [])
        if not items:
            break
        for p in items:
            pid = p.get('id')
            if pid in seen:
                continue
            seen.add(pid)
            variants = p.get('variants') or []
            active = [v for v in variants if v.get('available', True)] or variants
            prices = [(money(v.get('price')), v) for v in active if money(v.get('price'))]
            if not prices:
                continue
            price, chosen = min(prices, key=lambda x:x[0])
            compare_prices = [money(v.get('compare_at_price')) for v in variants if money(v.get('compare_at_price'))]
            title = clean(p.get('title')) or 'Sauna'
            vendor = clean(p.get('vendor')) or 'Unknown'
            handle = clean(p.get('handle')) or slug(title)
            sku = clean(chosen.get('sku'))
            body = BeautifulSoup(p.get('body_html') or '', 'html.parser').get_text('\n', strip=True) if BeautifulSoup else re.sub('<[^>]+>', '\n', p.get('body_html') or '')
            combined = '\n'.join([title, str(p.get('tags') or ''), body])
            specs = parse_specs(combined)
            images = p.get('images') or []
            image = images[0].get('src') if images else None
            key = slug(sku or handle)
            item = {
                'model_key': key,
                'brand': vendor,
                'model': sku or infer_model_from_title(title) or handle,
                'title': title,
                'price': price,
                'reference_price': max(compare_prices) if compare_prices else None,
                'inhouse_url': f'{BASE}/products/{handle}',
                'image': image,
                'source_urls': [f'{BASE}/products/{handle}'],
                **specs,
            }
            all_products.append(item)
        if len(items) < 250:
            break
    return all_products


def infer_model_from_title(title):
    t = title.upper().replace('‑','-').replace('–','-')
    pats = [r'\bCTC[A-Z0-9-]+\b', r'\bFD-(?:KN)?0?[1-9]\b', r'\bFD-?[1-9]\b', r'\bDYN-[A-Z0-9-]+(?:\s+ELITE)?\b', r'\bMX-[A-Z0-9-]+(?:\s+(?:CED|HEM|FS|ZF))?\b', r'\bGDI-[A-Z0-9-]+(?:\s+ELITE)?\b', r'\b(?:EE|E|G|CL|X)\d+[A-Z0-9-]*\b', r'\bMW\d+[A-Z0-9-]*\b', r'\bIS-[1-5]\b']
    for p in pats:
        m = re.search(p, t)
        if m:
            return m.group(0)
    return None


def fetch_page_specs(url):
    s = session()
    r = s.get(url, timeout=35)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, 'html.parser')
    text = soup.get_text('\n', strip=True)
    specs = parse_specs(text)
    # JSON-LD can add product identity / image where useful.
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            obj = json.loads(tag.get_text(strip=True) or '{}')
        except Exception:
            continue
        nodes = obj if isinstance(obj, list) else [obj]
        for node in nodes:
            if isinstance(node, dict) and '@graph' in node:
                nodes.extend(node.get('@graph') or [])
        for node in nodes:
            if not isinstance(node, dict):
                continue
            typ = node.get('@type')
            types = typ if isinstance(typ, list) else [typ]
            if 'Product' in types:
                if not specs.get('image'):
                    image = node.get('image')
                    if isinstance(image, list): image = image[0] if image else None
                    if isinstance(image, dict): image = image.get('url')
                    if image: specs['image'] = image
                return specs
    return specs


def load_manufacturer_sources():
    if not MANUFACTURERS.exists():
        return []
    with MANUFACTURERS.open(newline='') as f:
        return [r for r in csv.DictReader(f) if str(r.get('active','1')).strip().lower() not in ('0','false','no')]


def enrich_from_sources(products):
    bykey = {p['model_key']: p for p in products}
    for row in load_manufacturer_sources():
        key = slug(row.get('model_key',''))
        url = clean(row.get('url'))
        target = bykey.get(key) if key else None
        if target is None and clean(row.get('match_text')):
            needle = clean(row.get('match_text')).lower()
            target = next((p for p in products if needle in ((p.get('model') or '') + ' ' + (p.get('title') or '')).lower()), None)
        if not url or target is None:
            continue
        urls = target.setdefault('source_urls', [])
        if url not in urls:
            urls.append(url)
        fetch_page = str(row.get('fetch_page', '1')).strip().lower() not in ('0','false','no','off')
        if not fetch_page:
            print(f'Registered source for {target.get("model_key")} from {row.get("source_name") or url}')
            continue
        try:
            specs = fetch_page_specs(url)
            key = target.get('model_key')
            for field, value in specs.items():
                if field == 'image':
                    if not target.get('image'):
                        target['image'] = value
                elif not target.get(field):
                    target[field] = value
            print(f'Enriched {key} from {row.get("source_name") or url}')
        except Exception as e:
            print(f'Enrichment skipped {key}: {e}', file=sys.stderr)
        time.sleep(.35)
    return products


def merge_existing(live, old):
    old_products = old.get('products', [])
    oldmap = {p.get('model_key'): p for p in old_products}
    protected = ['capacity','type','placement','emf','spectrum','red_light','voltage','amperage','plug','ir_wattage','heater','heater_kw','max_temp','exterior_dimensions','interior_dimensions','wood','weight','warranty','image','reference_price','retailer_offers']
    for p in live:
        prior = oldmap.get(p.get('model_key'))
        if prior:
            for field in protected:
                if not p.get(field) and prior.get(field) not in (None,''):
                    p[field] = prior[field]
            oldurls = prior.get('source_urls') or []
            urls = p.setdefault('source_urls', [])
            for u in oldurls:
                if u not in urls:
                    urls.append(u)

    # Retailer/manufacturer-only models are not present in the InHouse Shopify
    # response. Keep them in the seed between runs even if an outside catalog is
    # temporarily unavailable, then let retailer enrichment refresh them.
    live_keys = {p.get('model_key') for p in live}
    for prior in old_products:
        key = prior.get('model_key')
        if key and key not in live_keys and not prior.get('inhouse_url'):
            live.append(prior)
            live_keys.add(key)
    return live


def h(v):
    return html.escape(str(v)) if v not in (None,'') else ''


def fmt_price(v):
    return f'${v:,.0f}' if isinstance(v, (int,float)) else 'Price not verified'


def display_model(p):
    raw = str(p.get('model') or '')
    return raw.split('|', 1)[0].strip() if '|' in raw else raw


def spec_value(p, field):
    v = p.get(field)
    if field == 'red_light':
        return 'Yes' if v is True else ('No' if v is False else 'Not verified')
    if field == 'capacity' and v:
        return f'{v} person' + ('s' if int(v) != 1 else '')
    if field in ('emf','spectrum','red_light','ir_wattage') and p.get('type') == 'Traditional':
        return 'Not applicable'
    return str(v) if v not in (None,'') else 'Not verified'


CORE_FIELDS = ['type','placement','capacity','voltage','amperage','plug','exterior_dimensions','wood','weight','warranty']
IR_FIELDS = ['emf','spectrum','red_light','ir_wattage']


def applicable_fields(p):
    fields = list(CORE_FIELDS)
    if p.get('type') in ('Infrared','Hybrid'):
        fields += IR_FIELDS
    if p.get('type') in ('Traditional','Hybrid'):
        fields += ['heater','heater_kw','max_temp']
    return fields


def completeness(p):
    fields = applicable_fields(p)
    verified = sum(p.get(field) not in (None,'') for field in fields)
    return round(verified * 100 / len(fields)) if fields else 0


def normalize_products(products):
    """Apply conservative public taxonomy rules before pages are generated."""
    unique = {}
    for p in products:
        if p.get('brand') == 'Almost Heaven Staging':
            p['brand'] = 'Almost Heaven Saunas'
        identity = f"{p.get('title','')} {p.get('model','')}".lower()
        if any(term in identity for term in ['far infrared','far ir sauna','full spectrum','infrared sauna']):
            p['type'] = 'Infrared'
        elif 'traditional sauna' in identity and 'hybrid' not in identity:
            p['type'] = 'Traditional'
        p['data_completeness'] = completeness(p)
        key = p.get('model_key')
        if key not in unique:
            unique[key] = p
            continue
        current = unique[key]
        for url in p.get('source_urls') or []:
            if url not in current.setdefault('source_urls',[]): current['source_urls'].append(url)
        seen={(o.get('retailer'),o.get('url')) for o in current.get('retailer_offers') or []}
        for offer in p.get('retailer_offers') or []:
            if (offer.get('retailer'),offer.get('url')) not in seen:
                current.setdefault('retailer_offers',[]).append(offer)
    return list(unique.values())


HEAD = '''<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/assets/style.css"><link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>∞</text></svg>">'''
HEADER = '''<header class="site-header"><div class="wrap nav"><a class="brand" href="/"><span class="mark">∞</span><span>Infinite Sauna</span></a><nav><a href="/database/">Database</a><a href="/compare/">Compare</a><a href="/electrical/">Electrical</a><a href="/data/">Data</a><a href="/methodology/">Methodology</a></nav></div></header>'''
FOOTER = '''<footer><div class="wrap footer-grid"><div><a class="brand" href="/"><span class="mark">∞</span><span>Infinite Sauna</span></a><p>An independent, source-documented sauna specification database updated weekly.</p></div><div><strong>Research</strong><a href="/database/">Model database</a><a href="/electrical/">Electrical database</a><a href="/changes/">Latest release</a><a href="/data/">Download data</a></div><div><strong>About the data</strong><a href="/methodology/">Methodology</a><a href="/retailers/">Retail sources</a><a href="/guides/120v-vs-240v/">Electrical guide</a><a href="/llms.txt">llms.txt</a></div></div></footer>'''


def load_retailer_profiles():
    if not RETAILERS.exists():
        return []
    with RETAILERS.open(newline='') as f:
        return list(csv.DictReader(f))


def nofollow_home_links(markup):
    """External links carry rel values when rendered; preserve internal crawl paths."""
    return markup


def retailer_offers(p):
    offers = [o for o in (p.get('retailer_offers') or []) if o.get('retailer') and o.get('url')]
    if not offers and p.get('inhouse_url'):
        offers = [{'retailer':'InHouse Wellness','url':p.get('inhouse_url'),'price':p.get('price'),'reference_price':p.get('reference_price'),'source_type':'featured'}]
    return offers


def featured_offer(p):
    offers = retailer_offers(p)
    ih = next((o for o in offers if (o.get('retailer') or '').lower() == 'inhouse wellness'), None)
    return ih or (offers[0] if offers else None)


def offer_link_rel(offer):
    return 'sponsored noopener' if (offer.get('retailer') or '').lower() == 'inhouse wellness' else 'nofollow noopener'


def retailer_offer_html(p, compact=False):
    offers = retailer_offers(p)
    if not offers:
        return '<span class="muted-offer">No current retail offer captured</span>'
    ih = next((o for o in offers if (o.get('retailer') or '').lower() == 'inhouse wellness' and isinstance(o.get('price'), (int,float))), None)
    ih_price = ih.get('price') if ih else None
    lines = []
    for offer in offers:
        price = fmt_price(offer.get('price'))
        delta = ''
        if ih_price and isinstance(offer.get('price'), (int,float)) and (offer.get('retailer') or '').lower() != 'inhouse wellness':
            diff = offer['price'] - ih_price
            if abs(diff) >= 1:
                direction = 'higher' if diff > 0 else 'lower'
                delta = f'<span class="offer-delta">{fmt_price(abs(diff))} {direction} than InHouse</span>'
        badge = '<span class="offer-badge">Featured</span>' if (offer.get('retailer') or '').lower() == 'inhouse wellness' else ''
        lines.append(f'<div class="offer-line"><a href="{h(offer.get("url"))}" target="_blank" rel="{offer_link_rel(offer)}"><strong>{h(offer.get("retailer"))}</strong></a>{badge}<span class="offer-price">{price}</span>{delta}</div>')
        if compact and len(lines) >= 2:
            break
    if compact and len(offers) > len(lines):
        lines.append(f'<span class="offer-more">+{len(offers)-len(lines)} more price source(s)</span>')
    return ''.join(lines)


def model_page(p):
    fields = [
        ('Type','type'),('Placement','placement'),('Capacity','capacity'),('Electrical','voltage'),('Amperage','amperage'),('Plug','plug'),
        ('EMF category','emf'),('Infrared spectrum','spectrum'),('Red light','red_light'),('IR wattage','ir_wattage'),('Heater','heater'),('Heater output','heater_kw'),
        ('Max temperature','max_temp'),('Exterior dimensions','exterior_dimensions'),('Interior dimensions','interior_dimensions'),('Wood / materials','wood'),('Weight','weight'),('Warranty','warranty')
    ]
    specs = ''.join(f'<div class="spec-row"><span>{h(label)}</span><strong>{h(spec_value(p,key))}</strong></div>' for label,key in fields)
    srcs = ''.join(f'<li><a href="{h(u)}" target="_blank" rel="nofollow noopener">{h(u.split("/")[2])}</a></li>' for u in p.get('source_urls',[]) if u)
    img = f'<img src="{h(p.get("image"))}" alt="{h(p.get("title"))}">' if p.get('image') else '<div class="image-placeholder">∞</div>'
    feat = featured_offer(p)
    feat_name = h(feat.get('retailer')) if feat else 'No current retailer captured'
    feat_price = fmt_price(feat.get('price')) if feat else fmt_price(p.get('price'))
    feat_button = f'<a class="btn primary" href="{h(feat.get("url"))}" target="_blank" rel="{offer_link_rel(feat)}">Check price at {feat_name}</a>' if feat else ''
    offer_schema = []
    for offer in retailer_offers(p):
        if isinstance(offer.get('price'), (int,float)):
            offer_schema.append({'@type':'Offer','priceCurrency':'USD','price':offer.get('price'),'url':offer.get('url'),'seller':{'@type':'Organization','name':offer.get('retailer')}})
    schema = {
        '@context':'https://schema.org','@type':'Product','name':p.get('title'),'sku':p.get('model'),'brand':{'@type':'Brand','name':p.get('brand')},
        'image':p.get('image') or None,'offers':offer_schema or None
    }
    schema = {k:v for k,v in schema.items() if v is not None}
    offers_html = retailer_offer_html(p)
    score = completeness(p)
    verified = sum(p.get(field) not in (None,'') for field in applicable_fields(p))
    total = len(applicable_fields(p))
    return f'''<!doctype html><html lang="en"><head>{HEAD}<title>{h(p.get('brand'))} {h(p.get('model'))} Specs & Comparison | Infinite Sauna</title><meta name="description" content="Source-documented specifications for {h(p.get('title'))}: electrical requirements, dimensions, capacity, heating type and current retailer prices."><link rel="canonical" href="https://infinitesauna.com/models/{h(p.get('model_key'))}/"><script type="application/ld+json">{json.dumps(schema)}</script></head><body>{HEADER}<main><section class="model-hero"><div class="wrap model-hero-grid"><div class="product-image">{img}</div><div><span class="eyebrow">Sauna specification record</span><h1>{h(p.get('brand'))} {h(display_model(p))}</h1><p class="lede">{h(p.get('title'))}</p><div class="chips"><span>{h(spec_value(p,'type'))}</span><span>{h(spec_value(p,'placement'))}</span><span>{h(spec_value(p,'capacity'))}</span><span class="confidence-chip">{score}% data complete</span></div><p class="record-meta">{verified} of {total} applicable core fields verified · refreshed {h(datetime.now(timezone.utc).date().isoformat())}</p><div class="buy-box"><div><span class="micro">Current featured source</span><strong>{feat_name}</strong><small>Observed price: {feat_price}</small></div>{feat_button}</div></div></div></section><section class="section"><div class="wrap two-col"><div><div class="section-title"><span class="eyebrow">Normalized specifications</span><h2>What the current sources verify</h2></div><div class="spec-table">{specs}</div><p class="note"><strong>Not verified</strong> means no reliable model-specific value was found. <strong>Not applicable</strong> means the field does not apply to this heat type. Manufacturer marketing terms such as “near zero EMF” are reported as claims, not independent measurements.</p><div class="retail-panel"><span class="eyebrow">Observed offers</span><h2>Current retail sources</h2>{offers_html}<p class="note">Prices are source snapshots and can change. Compare exact heater packages, wood choices, shipping and options before treating two offers as equivalent.</p></div></div><aside class="panel"><h3>Compare this model</h3><p>Put this record beside up to three alternatives.</p><a class="btn secondary" href="/compare/?models={h(p.get('model_key'))}">Compare {h(display_model(p))}</a><hr><h3>Source record</h3><ul class="source-list">{srcs or '<li>Retail catalog source</li>'}</ul><p class="micro">Source priority: manufacturer documentation, manufacturer page, authorized retailer, then secondary retailer.</p><a class="text-link" href="/methodology/">Verification rules →</a></aside></div></section></main>{FOOTER}</body></html>'''


def brand_page(brand, products):
    bslug = slug(brand)
    cards = ''.join(card_html(p, compact=True) for p in products)
    prices = [p.get('price') for p in products if isinstance(p.get('price'),(int,float))]
    volts = sum(bool(p.get('voltage')) for p in products)
    avg_complete = round(sum(completeness(p) for p in products)/len(products)) if products else 0
    low, high = (min(prices), max(prices)) if prices else (None,None)
    price_range = f'{fmt_price(low)}–{fmt_price(high)}' if prices else 'Not enough verified prices'
    return f'''<!doctype html><html lang="en"><head>{HEAD}<title>{h(brand)} Sauna Models, Specs & Price Range | Infinite Sauna</title><meta name="description" content="A source-documented reference for {h(brand)} sauna models, electrical requirements, dimensions, heat types and observed prices."><link rel="canonical" href="https://infinitesauna.com/brands/{bslug}/"></head><body>{HEADER}<main><section class="page-hero"><div class="wrap"><span class="eyebrow">Manufacturer intelligence</span><h1>{h(brand)} sauna models</h1><p>{len(products)} current records, normalized into the same specification schema and refreshed weekly.</p><div class="stat-strip"><div><strong>{len(products)}</strong><span>models tracked</span></div><div><strong>{volts}</strong><span>with voltage documented</span></div><div><strong>{avg_complete}%</strong><span>average completeness</span></div><div><strong>{price_range}</strong><span>observed price range</span></div></div></div></section><section class="section"><div class="wrap"><div class="section-title"><span class="eyebrow">Current records</span><h2>Compare {h(brand)} specifications</h2><p>Capacity is manufacturer-stated unless otherwise noted. Pricing and configuration can change; follow each record to its source list.</p></div><div class="card-grid">{cards}</div></div></section></main>{FOOTER}</body></html>'''


def retailers_page(products):
    counts = {}
    for product in products:
        for offer in retailer_offers(product):
            name = offer.get('retailer')
            if name:
                counts[name] = counts.get(name, 0) + 1
    profiles = load_retailer_profiles()
    cards = []
    for profile in profiles:
        name = clean(profile.get('retailer'))
        url = clean(profile.get('url'))
        if not name or not url:
            continue
        count = counts.get(name, 0)
        coverage = f'{count} model' + ('' if count == 1 else 's') + ' currently indexed' if count else 'Catalog source configured'
        cards.append(f'''<article class="retailer-card"><span class="eyebrow">{h(profile.get('brand_focus') or 'Sauna retailer')}</span><h2>{h(name)}</h2><p>{h(profile.get('description'))}</p><div class="retailer-card-foot"><span>{h(coverage)}</span><a class="btn secondary" href="{h(url)}" target="_blank" rel="nofollow sponsored noopener">Visit {h(name)}</a></div></article>''')
    return f'''<!doctype html><html lang="en"><head>{HEAD}<title>Sauna Retailers in the Infinite Sauna Database</title><meta name="description" content="Learn about every sauna retailer and manufacturer-direct store represented in the Infinite Sauna model database."><link rel="canonical" href="https://infinitesauna.com/retailers/"></head><body>{HEADER}<main><section class="page-hero"><div class="wrap"><span class="eyebrow">Retail directory</span><h1>Sauna retailers represented</h1><p>These retailers and manufacturer-direct stores provide the product listings used in our model database. InHouse Wellness remains the featured destination whenever it carries the brand; outside retailers are used to add brands not available there or to document an exact-model comparison source.</p></div></section><section class="section"><div class="wrap"><div class="retailer-directory">{''.join(cards)}</div><p class="note">Retail links may be commercial. Product availability, configuration, shipping and pricing can change; confirm details directly with the retailer before purchasing.</p></div></section></main>{FOOTER}</body></html>'''


def card_html(p, compact=False):
    img = f'<img loading="lazy" src="{h(p.get("image"))}" alt="{h(p.get("title"))}">' if p.get('image') else '<div class="image-placeholder small">∞</div>'
    attrs = ' '.join([
        f'data-brand="{h(p.get("brand"))}"', f'data-type="{h(p.get("type"))}"', f'data-placement="{h(p.get("placement"))}"',
        f'data-capacity="{h(p.get("capacity"))}"', f'data-voltage="{h(p.get("voltage"))}"', f'data-emf="{h(p.get("emf"))}"',
        f'data-search="{h((p.get("title") or "") + " " + (p.get("model") or "") + " " + (p.get("brand") or ""))}"'
    ])
    compare = '' if compact else f'<label class="compare-check"><input type="checkbox" data-compare="{h(p.get("model_key"))}"> Compare</label>'
    feat = featured_offer(p)
    retailer = h(feat.get('retailer')) if feat else 'Price source'
    link = f'<a class="text-link" href="{h(feat.get("url"))}" target="_blank" rel="{offer_link_rel(feat)}">{retailer} →</a>' if feat else '<a class="text-link" href="/models/{}/">See sources →</a>'.format(h(p.get('model_key')))
    offers = retailer_offers(p)
    retail_links = ''.join(f'<a href="{h(o.get("url"))}" target="_blank" rel="{offer_link_rel(o)}">{h(o.get("retailer"))}</a>' for o in offers[:3])
    if len(offers) > 3:
        retail_links += f'<span>+{len(offers)-3} more</span>'
    if not retail_links:
        retail_links = '<span class="retailer-none">No retailer captured</span>'
    return f'''<article class="sauna-card" {attrs}><a class="thumb" href="/models/{h(p.get('model_key'))}/">{img}</a><div class="card-body"><div class="card-top"><span class="model-code">{h(display_model(p))}</span>{compare}</div><h3><a href="/models/{h(p.get('model_key'))}/">{h(p.get('title'))}</a></h3><div class="mini-specs"><span>{h(spec_value(p,'type'))}</span><span>{h(spec_value(p,'capacity'))}</span><span>{h(spec_value(p,'voltage'))}</span><span>{h(spec_value(p,'emf'))}</span></div><div class="card-retailers"><span class="micro">Retailers captured</span><div class="retailer-mini-list">{retail_links}</div></div><div class="card-foot"><div><span class="micro">Featured price</span><strong>{fmt_price(p.get('price'))}</strong></div>{link}</div></div></article>'''


def comparison_page(a, b):
    fields = [('Type','type'),('Placement','placement'),('Capacity','capacity'),('Voltage','voltage'),('Amperage','amperage'),('Plug','plug'),('EMF','emf'),('Spectrum','spectrum'),('Red light','red_light'),('Heater','heater'),('Max temp','max_temp'),('Exterior size','exterior_dimensions'),('Interior size','interior_dimensions'),('Wood','wood'),('Warranty','warranty')]
    rows = ''.join(f'<tr><th>{h(label)}</th><td>{h(spec_value(a,key))}</td><td>{h(spec_value(b,key))}</td></tr>' for label,key in fields)
    slugname = f'{a["model_key"]}-vs-{b["model_key"]}'
    def head(product):
        href = product.get('inhouse_url') or f'/models/{product.get("model_key")}/'
        attrs = ' target="_blank" rel="sponsored noopener"' if product.get('inhouse_url') else ''
        note = 'View at InHouse Wellness ↗' if product.get('inhouse_url') else 'View model specs'
        return f'<a class="compare-model-link" href="{h(href)}"{attrs}><span>{h(product.get("brand"))}</span><strong>{h(display_model(product))}</strong></a><span class="compare-inhouse-note">{note}</span>'
    return f'''<!doctype html><html lang="en"><head>{HEAD}<title>{h(a.get('brand'))} {h(display_model(a))} vs {h(b.get('brand'))} {h(display_model(b))} | Infinite Sauna</title><meta name="description" content="Compare {h(display_model(a))} and {h(display_model(b))} sauna specifications and retailer prices side by side."><link rel="canonical" href="https://infinitesauna.com/comparisons/{slugname}/"></head><body>{HEADER}<main><section class="page-hero"><div class="wrap"><span class="eyebrow">Sauna comparison</span><h1>{h(display_model(a))} vs {h(display_model(b))}</h1><p>{h(a.get('brand'))} {h(display_model(a))} and {h(b.get('brand'))} {h(display_model(b))} compared using fields and retail sources our database currently verifies.</p></div></section><section class="section"><div class="wrap"><div class="comparison-scroll"><table class="comparison-table" data-count="2"><colgroup><col class="comparison-spec-col"><col class="comparison-model-col"><col class="comparison-model-col"></colgroup><thead><tr><th>Specification</th><th>{head(a)}</th><th>{head(b)}</th></tr></thead><tbody>{rows}<tr><th>Retail offers</th><td>{retailer_offer_html(a)}</td><td>{retailer_offer_html(b)}</td></tr></tbody></table></div><p class="note">No universal “winner” is assigned. Electrical service, space, preferred heat type, capacity, configuration and delivered price can make different models better fits.</p></div></section></main>{FOOTER}</body></html>'''


def write_csv(products):
    fields = ['model_key','brand','model','title','price','reference_price','type','placement','capacity','voltage','amperage','plug','emf','spectrum','red_light','ir_wattage','heater','heater_kw','max_temp','exterior_dimensions','interior_dimensions','wood','weight','warranty','data_completeness','inhouse_url','retailer_count','retailers']
    with CSV_OUT.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for p in products:
            row = {k:p.get(k) for k in fields}; row['retailer_count'] = len(retailer_offers(p)); row['retailers'] = ' | '.join(o.get('retailer','') for o in retailer_offers(p)); w.writerow(row)


def home_page(products):
    template = (ROOT / 'templates' / 'index.template.html').read_text()
    brands = sorted(set(p.get('brand') for p in products if p.get('brand')))

    # Rotate the unfiltered homepage across retailer buckets so the first screen
    # is not dominated by a single source. JavaScript repeats this against the
    # live JSON, but this also makes the generated HTML itself diverse.
    buckets = {}
    for product in products:
        offer = featured_offer(product)
        name = (offer or {}).get('retailer') or 'No retailer'
        buckets.setdefault(name, []).append(product)
    rng = random.Random(datetime.now(timezone.utc).date().isoformat())
    for bucket in buckets.values():
        rng.shuffle(bucket)
    retailer_names = list(buckets)
    rng.shuffle(retailer_names)
    mixed = []
    while any(buckets.values()):
        for name in retailer_names:
            if buckets[name]:
                mixed.append(buckets[name].pop())
    cards = ''.join(card_html(p) for p in mixed)

    brandcloud = ''.join(f'<a href="/brands/{slug(b)}/">{h(b)}</a>' for b in brands)
    retailer_map = {}
    for product in products:
        for offer in retailer_offers(product):
            name = offer.get('retailer')
            if not name:
                continue
            entry = retailer_map.setdefault(name, {'count':0, 'url':offer.get('url')})
            entry['count'] += 1
    retailer_items = list(retailer_map.items())
    rng.shuffle(retailer_items)
    retailer_parts = []
    for name, value in retailer_items:
        parsed = urlparse(value.get('url') or '')
        destination = f'{parsed.scheme}://{parsed.netloc}' if parsed.scheme and parsed.netloc else (value.get('url') or '#')
        rel = 'sponsored noopener' if name.lower() == 'inhouse wellness' else 'nofollow noopener'
        plural = '' if value['count'] == 1 else 's'
        retailer_parts.append(f'<a href="{h(destination)}" target="_blank" rel="{rel}"><strong>{h(name)}</strong><span>{value["count"]} model{plural}</span></a>')
    retailercloud = ''.join(retailer_parts)
    page = (template
            .replace('{{MODEL_COUNT}}', str(len(products)))
            .replace('{{BRAND_COUNT}}', str(len(brands)))
            .replace('{{RETAILER_COUNT}}', str(len(retailer_map)))
            .replace('{{UPDATED}}', datetime.now(timezone.utc).date().isoformat())
            .replace('{{CARDS}}', cards)
            .replace('{{BRANDS}}', brandcloud)
            .replace('{{RETAILERS}}', retailercloud))
    return nofollow_home_links(page)


def listing_page(title, description, products, canonical, eyebrow='Research database'):
    cards = ''.join(card_html(p, compact=True) for p in products)
    prices = [p.get('price') for p in products if isinstance(p.get('price'),(int,float))]
    median = sorted(prices)[len(prices)//2] if prices else None
    brands = len(set(p.get('brand') for p in products if p.get('brand')))
    return f'''<!doctype html><html lang="en"><head>{HEAD}<title>{h(title)} | Infinite Sauna</title><meta name="description" content="{h(description)}"><link rel="canonical" href="https://infinitesauna.com/{canonical}/"></head><body data-page="listing">{HEADER}<main><section class="page-hero"><div class="wrap"><span class="eyebrow">{h(eyebrow)}</span><h1>{h(title)}</h1><p>{h(description)}</p><div class="stat-strip"><div><strong>{len(products)}</strong><span>matching models</span></div><div><strong>{brands}</strong><span>brands represented</span></div><div><strong>{fmt_price(median)}</strong><span>median observed price</span></div><div><strong>{round(sum(completeness(p) for p in products)/len(products)) if products else 0}%</strong><span>average completeness</span></div></div></div></section><section class="section"><div class="wrap"><div class="card-grid">{cards or '<p>No verified matching records in this release.</p>'}</div></div></section></main>{FOOTER}</body></html>'''


def database_page(products):
    cards = ''.join(card_html(p) for p in products)
    return f'''<!doctype html><html lang="en"><head>{HEAD}<title>Complete Sauna Model Database | Infinite Sauna</title><meta name="description" content="Search {len(products)} sauna models by brand, heat type, capacity, placement, voltage and EMF terminology."><link rel="canonical" href="https://infinitesauna.com/database/"></head><body data-page="database">{HEADER}<main><section class="page-hero compact-hero"><div class="wrap"><span class="eyebrow">Complete catalog</span><h1>Sauna model database</h1><p>Search every current record. Open a model to inspect sources, applicable fields and data completeness.</p></div></section><section class="section" id="database"><div class="wrap"><div class="filter-shell"><div class="search-row"><input id="q" type="search" placeholder="Search brand, model or SKU…" aria-label="Search sauna models"><select id="brand"></select><select id="type"></select><select id="placement"></select></div><div class="filter-row"><select id="capacity"></select><select id="voltage"></select><select id="emf"></select></div></div><div class="result-meta"><span id="resultCount">{len(products)} models shown</span><span>Choose up to four models to compare</span></div><div class="card-grid" id="cardGrid">{cards}</div></div></section></main><div class="compare-tray" id="compareTray"><div><strong id="compareCount">0 selected</strong><small> · choose up to four models</small></div><a class="btn primary" id="compareGo" href="/compare/">Compare selected</a></div>{FOOTER}<script src="/assets/app.js"></script></body></html>'''


def electrical_page(products):
    rows=[]
    for p in sorted(products,key=lambda x:((x.get('voltage') or 'ZZZ'),x.get('brand') or '',display_model(x))):
        if not p.get('voltage'):
            continue
        rows.append(f'''<tr><td><a href="/models/{h(p.get('model_key'))}/"><strong>{h(p.get('brand'))}</strong><br>{h(display_model(p))}</a></td><td>{h(spec_value(p,'type'))}</td><td>{h(spec_value(p,'voltage'))}</td><td>{h(spec_value(p,'amperage'))}</td><td>{h(spec_value(p,'plug'))}</td><td>{h(spec_value(p,'heater_kw'))}</td></tr>''')
    return f'''<!doctype html><html lang="en"><head>{HEAD}<title>Sauna Electrical Requirements Database | Infinite Sauna</title><meta name="description" content="Search documented voltage, amperage, plug and heater requirements for sauna models."><link rel="canonical" href="https://infinitesauna.com/electrical/"></head><body>{HEADER}<main><section class="page-hero"><div class="wrap"><span class="eyebrow">Installation research</span><h1>Sauna electrical database</h1><p>{len(rows)} models currently have a documented voltage. “120V” does not by itself mean an existing household receptacle or shared circuit is suitable.</p></div></section><section class="section"><div class="wrap"><div class="guide-callout"><strong>Before installation:</strong> verify the exact model manual, breaker size, receptacle, conductor, GFCI requirements and local code with the manufacturer and a licensed electrician.</div><div class="table-scroll"><table class="data-table"><thead><tr><th>Model</th><th>Heat type</th><th>Voltage</th><th>Amperage</th><th>Plug / connection</th><th>Heater output</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div><p class="note">Blank or “Not verified” values are intentionally not inferred from similar models.</p></div></section></main>{FOOTER}</body></html>'''


def changes_page(products, today):
    verified = sum(sum(p.get(f) not in (None,'') for f in applicable_fields(p)) for p in products)
    avg = round(sum(completeness(p) for p in products)/len(products)) if products else 0
    return f'''<!doctype html><html lang="en"><head>{HEAD}<title>Database Release {h(today)} | Infinite Sauna</title><meta name="description" content="Release summary and coverage metrics for the {h(today)} Infinite Sauna dataset."><link rel="canonical" href="https://infinitesauna.com/changes/"></head><body>{HEADER}<main><section class="page-hero"><div class="wrap"><span class="eyebrow">Dataset release</span><h1>Release {h(today)}</h1><p>A stable summary of what this weekly dataset contains and where verification work remains.</p><div class="stat-strip"><div><strong>{len(products)}</strong><span>unique model records</span></div><div><strong>{len(set(p.get('brand') for p in products))}</strong><span>canonical brands</span></div><div><strong>{verified:,}</strong><span>verified applicable fields</span></div><div><strong>{avg}%</strong><span>average completeness</span></div></div></div></section><section class="section"><div class="wrap guide-body"><h2>Coverage in this release</h2><p>{sum(bool(p.get('voltage')) for p in products)} records include voltage, {sum(bool(p.get('exterior_dimensions')) for p in products)} include exterior dimensions, {sum(bool(p.get('warranty')) for p in products)} include warranty information, and {sum(bool(p.get('source_urls')) for p in products)} link to at least one source.</p><h2>How to cite this release</h2><div class="citation-box">Infinite Sauna, “Sauna Model Database, release {h(today)},” accessed {h(today)}, https://infinitesauna.com/data/</div><h2>Known limitations</h2><p>Retail configurations can differ by heater, finish and accessories. Capacity is usually manufacturer-stated. EMF labels reproduce manufacturer terminology and are not independent laboratory measurements. Missing values remain unverified until a model-specific source is available.</p><p><a class="btn secondary" href="/data/">Download this dataset</a></p></div></section></main>{FOOTER}</body></html>'''


def data_page(products, today):
    return f'''<!doctype html><html lang="en"><head>{HEAD}<title>Download the Sauna Model Dataset | Infinite Sauna</title><meta name="description" content="Download the Infinite Sauna model database as CSV or JSON, with methodology and citation guidance."><link rel="canonical" href="https://infinitesauna.com/data/"><script type="application/ld+json">{json.dumps({'@context':'https://schema.org','@type':'Dataset','name':'Infinite Sauna Model Database','dateModified':today,'description':'Normalized specifications and observed prices for consumer sauna models.','url':'https://infinitesauna.com/data/','distribution':[{'@type':'DataDownload','encodingFormat':'text/csv','contentUrl':'https://infinitesauna.com/data/saunas.csv'},{'@type':'DataDownload','encodingFormat':'application/json','contentUrl':'https://infinitesauna.com/data/saunas.json'}]})}</script></head><body>{HEADER}<main><section class="page-hero"><div class="wrap"><span class="eyebrow">Open reference data</span><h1>Download the sauna dataset</h1><p>{len(products)} model records across {len(set(p.get('brand') for p in products))} canonical brands, refreshed {h(today)}.</p></div></section><section class="section"><div class="wrap download-grid"><article class="download-card"><span class="file-type">CSV</span><h2>Flat model table</h2><p>Best for spreadsheets, analysis and data journalism.</p><a class="btn primary" href="/data/saunas.csv" download>Download CSV</a></article><article class="download-card"><span class="file-type">JSON</span><h2>Structured records</h2><p>Includes nested source URLs and retailer offers.</p><a class="btn primary" href="/data/saunas.json" download>Download JSON</a></article><article class="download-card"><span class="file-type">DOCS</span><h2>Methods and limitations</h2><p>Read matching, source priority and verification rules.</p><a class="btn secondary" href="/methodology/">Read methodology</a></article></div><div class="guide-body"><h2>Citing Infinite Sauna</h2><div class="citation-box">Infinite Sauna, “Sauna Model Database,” release {h(today)}, https://infinitesauna.com/data/</div><p>You may analyze and quote summary statistics with attribution and a link to this dataset. Product images and linked source documents remain the property of their respective owners.</p></div></section></main>{FOOTER}</body></html>'''


def generate_pages(products):
    (ROOT / 'index.html').write_text(home_page(products))
    today = datetime.now(timezone.utc).date().isoformat()
    for dirname in ['database','electrical','changes','data']:
        (ROOT / dirname).mkdir(exist_ok=True)
    (ROOT / 'database' / 'index.html').write_text(database_page(products))
    (ROOT / 'electrical' / 'index.html').write_text(electrical_page(products))
    (ROOT / 'changes' / 'index.html').write_text(changes_page(products, today))
    (ROOT / 'data' / 'index.html').write_text(data_page(products, today))
    retailers_dir = ROOT / 'retailers'; retailers_dir.mkdir(exist_ok=True)
    (retailers_dir / 'index.html').write_text(retailers_page(products))
    models_dir = ROOT / 'models'; models_dir.mkdir(exist_ok=True)
    brands_dir = ROOT / 'brands'; brands_dir.mkdir(exist_ok=True)
    comps_dir = ROOT / 'comparisons'; comps_dir.mkdir(exist_ok=True)
    valid_model_dirs = set()
    for p in products:
        d = models_dir / p['model_key']; d.mkdir(parents=True, exist_ok=True); valid_model_dirs.add(p['model_key'])
        (d / 'index.html').write_text(model_page(p))
    for d in list(models_dir.iterdir()):
        if d.is_dir() and d.name not in valid_model_dirs:
            for x in d.iterdir(): x.unlink()
            d.rmdir()

    bybrand = {}
    for p in products:
        bybrand.setdefault(p.get('brand') or 'Unknown', []).append(p)
    valid_brands = set()
    for brand, items in bybrand.items():
        bs = slug(brand); valid_brands.add(bs); d = brands_dir / bs; d.mkdir(parents=True, exist_ok=True)
        (d / 'index.html').write_text(brand_page(brand, sorted(items, key=lambda x:(x.get('capacity') or 99, x.get('model') or ''))))
    for d in list(brands_dir.iterdir()):
        if d.is_dir() and d.name not in valid_brands:
            for x in d.iterdir(): x.unlink()
            d.rmdir()

    bykey = {p['model_key']: p for p in products}
    valid_comps = set()
    if COMPARISONS.exists():
        with COMPARISONS.open(newline='') as f:
            for row in csv.DictReader(f):
                akey, bkey = slug(row.get('model_a','')), slug(row.get('model_b',''))
                if akey in bykey and bkey in bykey and akey != bkey:
                    name = f'{akey}-vs-{bkey}'; valid_comps.add(name); d = comps_dir / name; d.mkdir(parents=True, exist_ok=True)
                    (d/'index.html').write_text(comparison_page(bykey[akey], bykey[bkey]))
    for d in list(comps_dir.iterdir()):
        if d.is_dir() and d.name not in valid_comps:
            for x in d.iterdir(): x.unlink()
            d.rmdir()

    category_defs = {
        '120v-saunas': ('120V Saunas', 'Sauna models with a documented 120V electrical specification.', lambda p:'120V' in str(p.get('voltage') or '')),
        '240v-saunas': ('240V Saunas', 'Sauna models with a documented 240V electrical specification.', lambda p:'240V' in str(p.get('voltage') or '')),
        'infrared-saunas': ('Infrared Saunas', 'Current infrared sauna records with normalized specifications and source links.', lambda p:p.get('type')=='Infrared'),
        'traditional-saunas': ('Traditional Saunas', 'Current traditional sauna records, including documented electric and wood-heated models.', lambda p:p.get('type')=='Traditional'),
        'outdoor-saunas': ('Outdoor Saunas', 'Sauna models identified for outdoor placement, compared by specifications and observed price.', lambda p:p.get('placement')=='Outdoor'),
        '2-person-saunas': ('2-Person Saunas', 'Sauna models with a manufacturer-stated capacity of two people.', lambda p:p.get('capacity')==2),
        '4-person-saunas': ('4-Person Saunas', 'Sauna models with a manufacturer-stated capacity of four people.', lambda p:p.get('capacity')==4),
    }
    for path,(title,description,test) in category_defs.items():
        d=ROOT/path; d.mkdir(exist_ok=True)
        (d/'index.html').write_text(listing_page(title,description,[p for p in products if test(p)],path))
    urls = ['https://infinitesauna.com/','https://infinitesauna.com/database/','https://infinitesauna.com/electrical/','https://infinitesauna.com/data/','https://infinitesauna.com/changes/','https://infinitesauna.com/compare/','https://infinitesauna.com/retailers/','https://infinitesauna.com/methodology/','https://infinitesauna.com/guides/120v-vs-240v/','https://infinitesauna.com/guides/infrared-vs-traditional/','https://infinitesauna.com/guides/emf-levels/']
    urls += [f'https://infinitesauna.com/{path}/' for path in category_defs]
    urls += [f'https://infinitesauna.com/models/{p["model_key"]}/' for p in products]
    urls += [f'https://infinitesauna.com/brands/{slug(b)}/' for b in bybrand]
    urls += [f'https://infinitesauna.com/comparisons/{c}/' for c in sorted(valid_comps)]
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + ''.join(f'<url><loc>{u}</loc><lastmod>{today}</lastmod></url>\n' for u in urls) + '</urlset>\n'
    (ROOT/'sitemap.xml').write_text(xml)


def main():
    old = load_existing()
    if '--seed-only' in sys.argv:
        products = normalize_products(old.get('products', []))
        old['products'] = products
        old['generated_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        DATA.write_text(json.dumps(old, indent=2, ensure_ascii=False) + '\n')
        write_csv(products); generate_pages(products)
        print(f'Generated pages for {len(products)} starter models.')
        return
    if not requests or not BeautifulSoup:
        raise SystemExit('Install requirements: requests beautifulsoup4')
    try:
        live = get_shopify_products()
    except Exception as e:
        print(f'Live Shopify retrieval failed: {e}', file=sys.stderr)
        live = []
    if live:
        products = merge_existing(live, old)
    else:
        print('Preserving existing database because live catalog could not be retrieved.', file=sys.stderr)
        products = old.get('products', [])
    products = enrich_from_sources(products)
    products = apply_verified_overrides(products, MANUFACTURERS, infer_model_from_title)
    products = enrich_retailers(products, ROOT, session(), BeautifulSoup, parse_specs, infer_model_from_title)
    products = normalize_products(products)
    products.sort(key=lambda p:((p.get('brand') or '').lower(), (p.get('model') or '').lower()))
    payload = {'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'currency':'USD', 'products':products}
    DATA.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n')
    write_csv(products); generate_pages(products)
    print(f'Wrote {len(products)} sauna models across {len(set(p.get("brand") for p in products))} brands.')


if __name__ == '__main__':
    main()
