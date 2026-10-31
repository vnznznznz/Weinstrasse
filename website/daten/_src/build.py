import json, re, glob, unicodedata, sys
SRC='/home/user/Weinstrasse/website/daten/_src/'
OUT='/home/user/Weinstrasse/website/daten/'
KAT={'S':'Schaumwein','W':'Weisswein','R':'Rosé','T':'Rotwein','D':'Süsswein','P':'Portwein','X':'Spirituose','A':'Alkoholfrei','O':'Olivenöl','C':'Cidre','Z':'Sonstiges'}
def num(s):
    s=s.strip()
    if not s: return None
    return float(s.replace('.','').replace(',','.'))
def slugify(s):
    s=s.replace('ß','ss').replace('ä','ae').replace('ö','oe').replace('ü','ue').replace('Ä','ae').replace('Ö','oe').replace('Ü','ue')
    s=unicodedata.normalize('NFKD',s).encode('ascii','ignore').decode()
    s=re.sub(r'[^a-zA-Z0-9]+','-',s).strip('-').lower()
    return s
# --- enrichment
enr={}
for fn in sorted(glob.glob(SRC+'katalog_*.txt')):
    for line in open(fn,encoding='utf-8'):
        line=line.rstrip('\n')
        if not line.strip(): continue
        p=[x.strip() for x in line.split('|')]
        while len(p)<6: p.append('')
        slug,name,uvp,reb,beschr,ausz=p[:6]
        uvpmap={}
        if uvp:
            for part in uvp.split(';'):
                part=part.strip()
                if '@' in part:
                    v,sz=part.split('@'); uvpmap[sz.strip()]=num(v)
                else: uvpmap['*']=num(part)
        enr[(slug,name)]={'uvp':uvpmap,'rebsorte':reb or None,
            'beschreibung':'; '.join(x.strip() for x in beschr.split(';') if x.strip()) or None,
            'auszeichnungen':[x.strip() for x in ausz.split(';;') if x.strip()]}
# --- products
prods=[]; cur=None; foot={}; region=None; used=set()
for fn in sorted(glob.glob(SRC+'produkte_*.txt')):
    for line in open(fn,encoding='utf-8'):
        line=line.rstrip('\n')
        if not line.strip(): continue
        if line.startswith('## '):
            slug,wg,land,rt=[x.strip() for x in line[3:].split('|')]
            cur={'slug':slug,'weingut':wg,'land':land}; foot={}; region=None; continue
        if line.startswith('@'):
            k,_,t=line[1:].partition(' '); foot[k]=t.strip(); continue
        if line.startswith('# '):
            region=line[2:].strip(); continue
        p=[x.strip() for x in line.split('|')]
        while len(p)<8: p.append('')
        name,jg,size,price,aktion,aktpreis,kat,hinweis=p[:8]
        m=re.match(r'^(PA|[\d.,]+)(\**[a-g]?)$',price)
        if not m: print('BAD PRICE',line); sys.exit(1)
        pv,star=m.group(1),m.group(2)
        lp=None if pv=='PA' else num(pv)
        zst=None
        if star:
            if star not in foot: print('MISSING FOOTNOTE',cur['slug'],star,line); sys.exit(1)
            zst=foot[star]
        hz=[h.strip() for h in hinweis.split(';') if h.strip()]
        bio=False
        if 'BIO' in hz: bio=True; hz.remove('BIO')
        if re.search(r'\bBIO\b|Bio ',name) : bio=True
        if 'Ecológico' in name: bio=True
        hw='; '.join(hz) or None
        e=enr.get((cur['slug'],name))
        if e: used.add((cur['slug'],name))
        uvp=None
        if e:
            uvp=e['uvp'].get(size) or e['uvp'].get('*')
        gcl=num(size) if size else None
        pid=slugify(f"{cur['slug']}-{name}-{jg or 'nv'}-{size or 'x'}")
        prods.append({'id':pid,'weingut':cur['weingut'],'weingut_slug':cur['slug'],'land':cur['land'],'region':region,
            'kategorie':KAT[kat],'name':name,'jahrgang':jg or None,'groesse_cl':gcl,'listenpreis':lp,'uvp':uvp,
            'aktion':aktion or None,'aktionspreis':num(aktpreis) if aktpreis else None,'zusatzsteuer':zst,
            'rebsorte':e['rebsorte'] if e else None,'beschreibung':e['beschreibung'] if e else None,
            'auszeichnungen':e['auszeichnungen'] if e else [],'bio':bio,'hinweis':hw})
# id uniqueness
ids={}
for pr in prods:
    ids.setdefault(pr['id'],[]).append(pr)
for k,v in ids.items():
    if len(v)>1:
        for i,pr in enumerate(v[1:],2): pr['id']=f"{pr['id']}-{i}"
unused=[k for k in enr if k not in used]
if unused: print('UNUSED ENRICHMENT:',*unused,sep='\n  ')
json.dump(prods,open(OUT+'produkte.json','w',encoding='utf-8'),ensure_ascii=False,indent=1)
# --- weingueter
wgs=[]; rec=None
def flush():
    if rec is None: return
    fr=[x.strip() for x in rec.get('fracht','').split('|') if x.strip()]
    wgs.append({'weingut_slug':rec['slug'],'name':rec['name'],'land':rec['land'],'region_titel':rec['region_titel'],
        'claim':rec.get('claim') or None,'portraet':rec.get('portraet') or None,
        'mindestmenge_flaschen':int(rec['mindestmenge']),'mindestmenge_text':rec['mindestmenge_text'],
        'frachtkosten':fr,'service':rec.get('service') or None,'abwicklung':rec['abwicklung'],
        'katalogseiten':rec['seiten'],'neuentdeckung':rec['neu']=='ja'})
for fn in sorted(glob.glob(SRC+'weingueter_*.txt')):
    for line in open(fn,encoding='utf-8'):
        line=line.rstrip('\n')
        if line.startswith('=== '):
            flush(); rec={'slug':line[4:].strip()}; continue
        if not line.strip(): continue
        k,_,v=line.partition(':'); rec[k.strip()]=v.strip()
flush()
json.dump(wgs,open(OUT+'katalog_weingueter.json','w',encoding='utf-8'),ensure_ascii=False,indent=1)
# --- stats
from collections import Counter
c=Counter(p['weingut_slug'] for p in prods)
print('Produkte gesamt:',len(prods))
for k,v in c.items(): print(f'  {k}: {v}')
print('ohne Listenpreis:',[p['name'] for p in prods if p['listenpreis'] is None])
print('mit UVP:',sum(1 for p in prods if p['uvp']),'mit Beschreibung:',sum(1 for p in prods if p['beschreibung']),'mit Auszeichnungen:',sum(1 for p in prods if p['auszeichnungen']))
print('Weingüter:',len(wgs),'ohne Porträt:',[w['weingut_slug'] for w in wgs if not w['portraet']])
pslugs=set(c); wslugs=set(w['weingut_slug'] for w in wgs)
print('Slugs nur in Produkten:',pslugs-wslugs,'nur in Weingütern:',wslugs-pslugs)
print('Kategorien:',Counter(p['kategorie'] for p in prods))
