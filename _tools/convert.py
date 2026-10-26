import html as htmllib
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from bs4 import BeautifulSoup, Comment, NavigableString

INV, WP, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
UPLOADS = os.path.join(WP, 'wp-content', 'uploads')
HOSTS = r'(?:https?:)?//(?:www\.)?(?:extemporanea\.eu|scienceground\.it)(?::\d+)?'
UPLOAD_RE = re.compile(HOSTS + r'/wp-content/uploads/([^"\'\s?#]+)')
SITE_RE = re.compile('^' + HOSTS + r'(/[^#]*)?(#.*)?$')
GREEK = {'ϱ': 'rho', 'ρ': 'rho', 'λ': 'lambda', 'β': 'beta', 'α': 'alpha'}

inv = {int(k): v for k, v in json.load(open(INV)).items()}
needed_uploads = set()


def ascii_slug(s):
    s = urllib.parse.unquote(s)
    s = ''.join(GREEK.get(c, c) for c in s)
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode()
    s = re.sub(r'[^a-zA-Z0-9-]+', '-', s).strip('-').lower()
    return s or 'post'


def norm_rel(rel):
    return urllib.parse.unquote(rel).strip('/')


# Pick one cached copy per post ID, and decide the output path.
items = []
for pid, copies in inv.items():
    e = copies[0]
    if e['type'] in ('attachment', 'archive'):
        continue
    rel = norm_rel(e['rel'])
    lang = 'en' if rel.startswith('en/') else 'it'
    base = rel[3:] if lang == 'en' else rel
    if e['type'] == 'page':
        kind = 'page'
        out = ('en/' if lang == 'en' else '') + ascii_slug(base) + '.qmd'
    elif e['type'].startswith('tribe_events'):
        kind = 'event'
        out = 'events/' + ascii_slug(base.split('/')[-1]) + '.qmd'
    else:
        kind = 'post'
        out = ('en/' if lang == 'en' else '') + 'posts/' + ascii_slug(base) + '.qmd'
    items.append(dict(pid=pid, kind=kind, lang=lang, out=out, copies=copies))

route = {}
for it in items:
    href = '/' + it['out'][:-4] + '.html'
    it['href'] = href
    route[it['pid']] = href
    for c in it['copies']:
        route[norm_rel(c['rel'])] = href

CATEGORY_NAMES = {}


PAGE_BASE = ['https://www.extemporanea.eu/']


def map_url(url, attr):
    url = (url or '').strip(' \t\n\u200e\u200f')
    if not url or url.startswith('#'):
        return url
    if url.startswith('//'):
        url = 'https:' + url
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:|^//', url):
        url = urllib.parse.urljoin(PAGE_BASE[0], url)
    m = UPLOAD_RE.match(url) if url.startswith(('http', '//')) else None
    if m:
        p = urllib.parse.unquote(m.group(1))
        needed_uploads.add(p)
        return '/uploads/' + p
    m = SITE_RE.match(url)
    if not m:
        return url
    path, frag = m.group(1) or '/', m.group(2) or ''
    q = urllib.parse.urlparse(path)
    key = norm_rel(q.path)
    pid = urllib.parse.parse_qs(q.query).get('p')
    if pid and int(pid[0]) in route:
        return route[int(pid[0])] + frag
    if key in route:
        return route[key] + frag
    if key in ('', 'en'):
        return '/' + (key + '/' if key else '') + 'index.html'
    if key.startswith(('category/', 'en/category/')):
        slug = key.rstrip('/').split('/')[-1]
        prefix = '/en/' if key.startswith('en/') else '/'
        return prefix + 'index.html#category=' + urllib.parse.quote(CATEGORY_NAMES.get(slug, slug))
    return url


def latex_of(img):
    return htmllib.unescape(img.get('alt', ''))


def clean_content(node, notes):
    for c in node.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()
    for sel in ['.wp-block-wptelegram-widget-join-channel', 'script', 'style', 'noscript',
                'iframe.wp-embedded-content', '.sharedaddy', 'form']:
        for t in node.select(sel):
            t.decompose()
    for t in node.find_all('iframe'):
        if 'wptelegram' in (t.get('src') or ''):
            t.decompose()

    for img in node.select('img.quicklatex-auto-format'):
        tex = latex_of(img)
        if 'ql-img-displayed-equation' in img.get('class', []):
            tex = re.sub(r'^\\\[|\\\]$', '', tex.strip())
            tex = re.sub(r'^\\begin\{equation\*?\}|\\end\{equation\*?\}$', '', tex.strip())
            marker = f'XXMATH{len(notes["math"])}XX'
            notes['math'].append('$$' + tex.strip() + '$$')
            p = img.find_parent('p', class_='ql-center-displayed-equation')
            if p:
                p.replace_with(BeautifulSoup(f'<p>{marker}</p>', 'lxml').p)
                continue
        else:
            marker = f'XXMATH{len(notes["math"])}XX'
            notes['math'].append('$' + tex.strip() + '$')
        img.replace_with(NavigableString(marker))
    for s in node.select('.ql-right-eqno, .ql-left-eqno'):
        s.decompose()

    wrapper = node.select_one('ol.easy-footnotes-wrapper')
    bodies = {}
    if wrapper:
        for li in wrapper.select('li.easy-footnote-single'):
            anchor = li.select_one('.easy-footnote-margin-adjust')
            m = re.search(r'easy-footnote-bottom-(\d+)', anchor.get('id', '')) if anchor else None
            for t in li.select('.easy-footnote-margin-adjust, .easy-footnote-to-top'):
                t.decompose()
            if m:
                bodies[m.group(1)] = li.decode_contents()
        wrapper.decompose()
    for s in node.select('span.easy-footnote-margin-adjust'):
        s.decompose()
    for span in node.select('span.easy-footnote'):
        a = span.find('a')
        m = re.search(r'easy-footnote-bottom-(\d+)', a.get('href', '')) if a else None
        if not m:
            continue
        n = m.group(1)
        body = bodies.get(n) or htmllib.unescape(a.get('title', ''))
        idx = len(notes['fn']) + 1
        notes['fn'].append(body)
        span.replace_with(NavigableString(f'XXFNREF{idx}XX'))

    for a in node.find_all('a', href=True):
        if re.search(HOSTS + r'/(?:en/)?(?:tag|author)/', a['href']) or '/login' in a['href']:
            a.unwrap()
        else:
            a['href'] = map_url(a['href'], 'href')
            if SITE_RE.match(a['href'].replace('%20', '')) and a.find('img'):
                a.unwrap()
    for img in node.find_all('img'):
        for k in ('srcset', 'sizes', 'loading', 'decoding', 'data-id', 'data-link'):
            img.attrs.pop(k, None)
        img['src'] = map_url(img.get('src', ''), 'src')
    for t in node.find_all('iframe'):
        t.clear()
    for t in node.find_all(['iframe', 'source', 'video', 'audio']):
        if t.get('src'):
            t['src'] = map_url(t['src'], 'src')
    for t in node.find_all(True):
        t.attrs.pop('style', None) if t.name not in ('iframe',) else None
    for t in node.find_all(['div', 'span']):
        t.unwrap()
    raw = notes.setdefault('raw', [])
    for t in node.find_all('iframe'):
        t['loading'] = 'lazy'
        marker = f'XXRAW{len(raw)}XX'
        raw.append(str(t))
        if t.parent.name in ('p', 'li', 'td', 'figure', 'blockquote'):
            t.replace_with(NavigableString(marker))
        else:
            t.replace_with(BeautifulSoup(f'<p>{marker}</p>', 'lxml').p)
    return node


def pandoc(html_fragment):
    r = subprocess.run(
        ['pandoc', '-f', 'html', '-t',
         'markdown-raw_attribute-native_divs-native_spans-bracketed_spans-fenced_divs-header_attributes-link_attributes-smart',
         '--wrap=none'],
        input=html_fragment, capture_output=True, text=True, check=True)
    return r.stdout


def to_markdown(node, notes):
    md = pandoc(node.decode_contents())
    for i, r in enumerate(notes.get('raw', [])):
        md = md.replace(f'XXRAW{i}XX', r)
    for i, m in enumerate(notes['math']):
        md = md.replace(f'XXMATH{i}XX', m)
    for i, body in enumerate(notes['fn'], 1):
        md = md.replace(f'XXFNREF{i}XX', f'[^{i}]')
    if notes['fn']:
        md += '\n'
        for i, body in enumerate(notes['fn'], 1):
            fnode = clean_content(BeautifulSoup(f'<div>{body}</div>', 'lxml').div, {'math': [], 'fn': []})
            text = pandoc(fnode.decode_contents()).strip().replace('\n\n', '\n\n    ')
            md += f'\n[^{i}]: {text}\n'
    md = re.sub(r'\n{3,}', '\n\n', md)
    return md.strip() + '\n'


def yaml_str(s):
    return json.dumps(s, ensure_ascii=False)


MONTHS_IT = {m: i for i, m in enumerate(['gennaio', 'febbraio', 'marzo', 'aprile', 'maggio', 'giugno', 'luglio',
                                         'agosto', 'settembre', 'ottobre', 'novembre', 'dicembre'], 1)}


def parse_date(txt):
    txt = txt.strip()
    for fmt in ('%d/%m/%Y', '%B %d, %Y', '%d %B %Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(txt, fmt).date().isoformat()
        except ValueError:
            pass
    m = re.match(r'(\d{1,2}) (\w+) (\d{4})', txt)
    if m and m.group(2).lower() in MONTHS_IT:
        return f'{m.group(3)}-{MONTHS_IT[m.group(2).lower()]:02d}-{int(m.group(1)):02d}'
    return None


def convert_comments(soup):
    root = soup.select_one('#comments .comments-inner')
    if not root:
        return ''
    out = []
    for c in root.select('div.comment'):
        body = c.find('article', class_='comment-body', recursive=False)
        if not body:
            continue
        depth = next((int(x[6:]) for x in c.get('class', []) if x.startswith('depth-')), 1)
        who = body.select_one('.comment-author .fn')
        when = body.select_one('.comment-metadata time')
        content = body.select_one('.comment-content')
        if content is None:
            continue
        notes = {'math': [], 'fn': []}
        text = to_markdown(clean_content(content, notes), notes).strip()
        name = who.get_text(strip=True) if who else 'Anonimo'
        date = when.get_text(strip=True) if when else ''
        block = f'**{name}** · *{date}*\n\n{text}'
        quote = '> ' * depth
        out.append('\n'.join((quote + line).rstrip() for line in block.split('\n')))
    return '\n\n'.join(out)


def convert(it):
    path = it['copies'][0]['path']
    PAGE_BASE[0] = 'https://%s/%s/' % (it['copies'][0]['host'], urllib.parse.quote(norm_rel(it['copies'][0]['rel'])))
    soup = BeautifulSoup(open(path, encoding='utf-8', errors='replace'), 'lxml')
    meta = {}
    if it['kind'] == 'event':
        ev = soup.select_one('.tribe-events-single') or soup.select_one('#tribe-events-content')
        title = ev.select_one('.tribe-events-single-event-title').get_text(strip=True)
        sched = ev.select_one('.tribe-events-schedule')
        start = sched.select_one('.tribe-event-date-start') if sched else None
        desc = ev.select_one('.tribe-events-single-event-description')
        details = ev.select_one('.tribe-events-event-meta')
        body = BeautifulSoup('<div></div>', 'lxml').div
        if sched:
            body.append(BeautifulSoup(f'<p><strong>{sched.get_text(" ", strip=True)}</strong></p>', 'lxml').p)
        img = ev.select_one('.tribe-events-event-image img')
        if img:
            body.append(img)
        if desc:
            body.append(desc)
        if details:
            for t in details.select('.tribe-events-gmap, .tribe-events-venue-map, script'):
                t.decompose()
            body.append(details)
        content = body
        dt = soup.find('meta', itemprop='startDate') or None
        m = re.search(r'"startDate":"(\d{4}-\d{2}-\d{2})', str(soup))
        meta['date'] = m.group(1) if m else None
        meta['categories'] = ['Eventi']
    else:
        art = soup.find('article')
        title_el = art.select_one('.entry-title')
        title = title_el.get_text(strip=True) if title_el else ''
        content = art.select_one('.entry-content')
        if it['kind'] == 'post':
            meta['categories'] = [a.get_text(strip=True) for a in art.select('.entry-categories-inner a')]
            for a in art.select('.entry-categories-inner a'):
                slug = a['href'].rstrip('/').split('/')[-1]
                CATEGORY_NAMES[slug] = a.get_text(strip=True)
            meta['keywords'] = [a.get_text(strip=True) for a in art.select('.post-tags a')]
            au = art.select_one('.post-author .meta-text a')
            meta['author'] = au.get_text(strip=True) if au else None
            dt = art.select_one('.post-date .meta-text a') or art.select_one('.post-date .meta-text')
            meta['date'] = parse_date(dt.get_text(strip=True)) if dt else None
            fi = art.select_one('figure.featured-media img')
            if fi:
                meta['image'] = map_url(fi.get('src'), 'src')
    notes = {'math': [], 'fn': []}
    md = to_markdown(clean_content(content, notes), notes)
    if meta.get('image'):
        md = f'![]({meta["image"]}){{.featured-image}}\n\n' + md
    comments = convert_comments(soup) if it['kind'] == 'post' else ''
    if comments:
        md += '\n---\n\n## ' + ('Comments' if it['lang'] == 'en' else 'Commenti') + '\n\n' + comments + '\n'

    fm = ['---', f'title: {yaml_str(title)}']
    if meta.get('author'):
        fm.append(f'author: {yaml_str(meta["author"])}')
    if meta.get('date'):
        fm.append(f'date: {meta["date"]}')
    if meta.get('categories'):
        fm.append('categories: [' + ', '.join(yaml_str(c) for c in meta['categories']) + ']')
    if meta.get('keywords'):
        fm.append('keywords: [' + ', '.join(yaml_str(c) for c in meta['keywords']) + ']')
    if meta.get('image'):
        fm.append(f'image: {yaml_str(meta["image"])}')
    if it['lang'] == 'en':
        fm.append('lang: en')
    fm.append(f'# source: {it["copies"][0]["host"]}/{norm_rel(it["copies"][0]["rel"])} (post id {it["pid"]})')
    fm.append('---')
    dest = os.path.join(OUT, it['out'])
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, 'w', encoding='utf-8') as f:
        f.write('\n'.join(fm) + '\n\n' + md)
    return it['out']


# Two passes so category slug→name mapping is complete before links are rewritten.
for it in items:
    if it['kind'] == 'post':
        s = open(it['copies'][0]['path'], encoding='utf-8', errors='replace').read()
        for slug, name in re.findall(r'/category/([^/"]+)/" rel="category tag">([^<]+)<', s):
            CATEGORY_NAMES[slug] = htmllib.unescape(name)

for it in sorted(items, key=lambda x: x['out']):
    print(convert(it))


def copy_upload(p):
    src = os.path.join(UPLOADS, p)
    dst = os.path.join(OUT, 'uploads', p)
    if not os.path.exists(src):
        alt = re.sub(r'-\d+x\d+(\.\w+)$', r'\1', src)
        if os.path.exists(alt):
            src = alt
        else:
            return p
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if not os.path.exists(dst):
        shutil.copyfile(src, dst)
    return None


with ThreadPoolExecutor(32) as ex:
    missing = [m for m in ex.map(copy_upload, sorted(needed_uploads)) if m]
print('uploads referenced:', len(needed_uploads), 'missing:', len(missing))
for m in missing:
    print('  MISSING', m)
json.dump(CATEGORY_NAMES, open(os.path.join(os.path.dirname(INV), 'categories.json'), 'w'), ensure_ascii=False, indent=1)
