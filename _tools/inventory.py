import os, re, sys, json
from bs4 import BeautifulSoup
root = sys.argv[1]
found = {}
for host in os.listdir(root):
    hd = os.path.join(root, host, 'all')
    if not os.path.isdir(hd): continue
    for dp, dn, fn in os.walk(hd):
        if 'index.html' not in fn: continue
        p = os.path.join(dp, 'index.html')
        rel = os.path.relpath(dp, hd)
        if '/feed' in '/'+rel or '/embed' in '/'+rel: continue
        try: html = open(p, encoding='utf-8', errors='replace').read()
        except OSError: continue
        m = re.search(r'<body class="([^"]*)"', html)
        if not m or 'singular' not in m.group(1).split(): continue
        cls = m.group(1)
        pid = re.search(r'(?:postid|page-id)-(\d+)', cls)
        if not pid:
            pid = re.search(r'<article class="post-(\d+)', html)
        if not pid: continue
        pid = int(pid.group(1))
        typ = 'page' if ' page ' in ' '+cls+' ' else ('post' if 'single-post' in cls else cls.split()[0])
        mt = os.path.getmtime(p)
        found.setdefault(pid, []).append(dict(host=host, rel=rel, type=typ, mtime=mt, path=p))
out = {pid: sorted(v, key=lambda x: -x['mtime']) for pid, v in found.items()}
json.dump(out, open(sys.argv[2], 'w'), ensure_ascii=False, indent=1)
from collections import Counter
print(len(out), Counter(v[0]['type'] for v in out.values()), Counter(v[0]['host'] for v in out.values()))
