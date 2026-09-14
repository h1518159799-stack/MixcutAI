import json,hashlib,urllib.request,shutil,os,time
from pathlib import Path
root=Path('/srv/mixcut/updates')
m=json.loads(Path('/tmp/mixcut-manifest-1.3.26.json').read_text())
assert m['version']=='1.3.26'
def digest(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1048576),b''): h.update(b)
 return h.hexdigest()
for name,meta in [('MixcutAI-v1.3.26.exe',m),('MixcutUpdater-v1.3.26.exe',m['updater'])]:
 src=Path('/tmp')/name; dst=root/name
 assert src.stat().st_size==meta['file_size'] and digest(src)==meta['sha256']
 if dst.exists(): assert digest(dst)==meta['sha256'],'published artifact differs'
 else:
  tmp=root/(name+'.new'); shutil.copyfile(src,tmp); tmp.chmod(0o644); os.replace(tmp,dst)
 url=meta.get('download_url') or meta['url']
 h=hashlib.sha256(); size=0
 with urllib.request.urlopen(url,timeout=90) as r:
  assert r.status==200
  for b in iter(lambda:r.read(1048576),b''): size+=len(b);h.update(b)
 assert size==meta['file_size'] and h.hexdigest()==meta['sha256']
 print(name,size,h.hexdigest(), 'HTTPS_VERIFIED',flush=True)
backup=root/('update.json.backup-'+time.strftime('%Y%m%d%H%M%S'))
shutil.copy2(root/'update.json',backup)
tmp=root/'update.json.new';tmp.write_text(json.dumps(m,ensure_ascii=False,indent=2));tmp.chmod(0o644)
json.loads(tmp.read_text());os.replace(tmp,root/'update.json')
print('MANIFEST_PUBLISHED',backup,flush=True)
