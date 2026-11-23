"""Collect the repository's public profiles independently from the Render service."""
import gzip
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from standalone import ROOT, collect_batch
from tach import PublicTach, extract_nickname

destination=ROOT/'history.json.gz'

def read(path):
    if not path.exists():
        return {}
    with gzip.open(path,'rt',encoding='utf8') as f:
        return json.load(f)

def save(path,data):
    temp=path.with_suffix('.tmp')
    with gzip.open(temp,'wt',encoding='utf8') as f:
        json.dump(data,f,ensure_ascii=False,separators=(',',':'))
    temp.replace(path)

def compact(batches,cutoff):
    # Keep the metadata in effect at the cutoff and later edits only.
    metadata={}
    kept=[]
    for batch in sorted(batches,key=lambda b:b['stamp']):
        changed=[]
        for post in batch['items']:
            if metadata.get(post['id'])!=post:
                changed.append(post)
                metadata[post['id']]=post
        if batch['stamp']>=cutoff:
            kept.append(dict(batch,items=changed if kept else list(metadata.values())))
    return kept

def main():
    cache=Path(os.environ.get('TACH_RUN_CACHE',str(destination)))
    history=read(destination)
    for nick,profile in read(cache).items():
        if nick not in history or profile['batches'][-1]['stamp']>history[nick]['batches'][-1]['stamp']:
            history[nick]=profile
    today=datetime.now(timezone.utc).date().isoformat()
    cutoff=(datetime.now(timezone.utc)-timedelta(days=90)).isoformat()
    names=[extract_nickname(n.strip()).lower() for n in (ROOT/'profiles.txt').read_text().splitlines() if n.strip()]
    failed=False
    for nick in dict.fromkeys(names):
        try:
            client=PublicTach()
            uid=client.resolve(nick)
            profile=history.get(nick,{})
            if profile.get('uid')!=uid:
                profile={'uid':uid,'batches':[]}
            full=profile.get('full_day')!=today
            batch=collect_batch(nick,uid,full,client)
            profile['batches']=compact(profile['batches']+[batch],cutoff)
            if full: profile['full_day']=today
            history[nick]=profile
        except Exception as exc:
            failed=True
            print(nick,type(exc).__name__)
    if not history:
        raise RuntimeError('Нет успешных замеров')
    history={nick:p for nick,p in history.items() if nick in names}
    save(cache,history)
    if cache!=destination:
        old=read(destination)
        if any(n not in old or not old[n]['batches'] or p['batches'][-1]['stamp'][:10]>old[n]['batches'][-1]['stamp'][:10] for n,p in history.items()):
            save(destination,history)
    return int(failed)

if __name__=='__main__':
    raise SystemExit(main())
