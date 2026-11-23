// IndexedDB keeps completed public measurements across Render restarts.
const DeviceHistory = (() => {
  const memory = new Map();
  let database;
  let unavailable = false;
  const metrics = ['views','likes','comments','shares'];
  function connection() {
    if (!database) database = new Promise((resolve,reject) => {
      const req = indexedDB.open('tach_public_history_45',1);
      req.onupgradeneeded = () => {
        const table = req.result.createObjectStore('batches',{keyPath:['uid','stamp']});
        table.createIndex('uid','uid');
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    return database;
  }
  async function merge(uid, incoming) {
    const local = memory.get(uid) || new Map();
    for (const b of incoming) if (b.uid===uid) local.set(b.stamp,b);
    memory.set(uid,local);
    try {
      const db = await connection();
      if(incoming.length) await new Promise((resolve,reject)=>{
        const tx=db.transaction('batches','readwrite');
        for(const b of incoming) if(b.uid===uid) tx.objectStore('batches').put(b);
        tx.oncomplete=resolve;tx.onabort=()=>reject(tx.error);
      });
      if(!local.hydrated) {
        const stored=await new Promise((resolve,reject)=>{
          const req=db.transaction('batches').objectStore('batches').index('uid').getAll(uid);
          req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error);
        });
        for(const b of stored)local.set(b.stamp,b);
        local.hydrated=true;
      }
    } catch(e) {unavailable=true;}
    return [...local.values()].sort((a,b)=>a.stamp.localeCompare(b.stamp));
  }
  const moscow=t=>new Date(Date.parse(t)+10800000).toISOString();
  function summarize(profile,batches,days) {
    const start=moscow(new Date(Date.now()-(days-1)*86400000).toISOString()).slice(0,10);
    const items=new Map(), last=new Map(),daily=new Map(),hourly=new Map(),scores=new Map(),audience=new Map(),details=new Map();
    const raw=[];
    let followers=null,first=null;
    for(const b of batches){
      for(const p of b.items) items.set(p.id,p);
      if(b.followers!=null){followers=b.followers;audience.set(moscow(b.stamp).slice(0,10),followers);}
      raw.push(...b.samples);
    }
    raw.sort((a,b)=>a[1].localeCompare(b[1]));
    for(const s of raw){
      const [id,t,...v]=s,prev=last.get(id),local=moscow(t),day=local.slice(0,10);
      if(prev && prev[1]===t)continue;
      first ||= t;
      const h=details.get(id)||new Map(),row=h.get(day)||{date:day,total:{},growth:null,reset:false,days_between:null};
      row.total=Object.fromEntries(metrics.map((k,i)=>[k,v[i]]));
      const d=daily.get(day)||{date:day,views:0,likes:0,comments:0,counts:[new Set(),new Set(),new Set(),new Set()]};
      if(prev){
        const delta=v.slice(0,3).map((val,i)=>val-prev[i+2]);
        const gap=(Date.parse(day)-Date.parse(moscow(prev[1]).slice(0,10)))/86400000;
        row.days_between=Math.max(row.days_between||0,gap);
        row.growth ||= {views:0,likes:0,comments:0};
        d.counts[0].add(id);
        if(gap>1)d.counts[2].add(id);
        if(delta.some(x=>x<0)){row.reset=true;d.counts[3].add(id);}
        delta.forEach((x,i)=>{row.growth[metrics[i]]+=Math.max(0,x);d[metrics[i]]+=Math.max(0,x);});
        if(day>=start){
          scores.set(id,(scores.get(id)||0)+Math.max(0,delta[0]));
          const hour=local.slice(0,13)+':00';hourly.set(hour,(hourly.get(hour)||0)+Math.max(0,delta[0]));
        }
      } else d.counts[1].add(id);
      h.set(day,row);details.set(id,h);daily.set(day,d);last.set(id,s);
    }
    const posts=[...items.values()].map(p=>{
      const s=last.get(p.id);
      return {...p,author:profile,observed_at:s?.[1],...Object.fromEntries(metrics.map((k,i)=>[k,s?.[i+2]??null]))};
    });
    const totals={};
    for(const k of metrics){const values=posts.map(p=>p[k]).filter(x=>x!=null);totals[k]=values.length&&(k!=='shares'||values.length===posts.length)?values.reduce((a,b)=>a+b,0):null;}
    return {profile,items:posts,totals,timezone:'Europe/Moscow',followers,first_observed_at:first,last_observed_at:batches.at(-1)?.stamp,
      daily_growth:[...daily.values()].filter(d=>d.date>=start).map(({counts,...d})=>({...d,comparable_posts:counts[0].size,new_posts:counts[1].size,gap_posts:counts[2].size,reset_posts:counts[3].size})),
      hourly_growth:[...hourly].map(([date,views])=>({date,views})),followers_history:[...audience].filter(([d])=>d>=start).map(([date,followers])=>({date,followers})),
      leaders:[...scores].filter(([,gain])=>gain>0).sort((a,b)=>b[1]-a[1]).slice(0,5).map(([id,gain])=>({id,gain,title:items.get(id)?.title})),
      details,raw,mode:'device',storageWarning:unavailable};
  }
  function csv(d){
    const titles=new Map(d.items.map(p=>[p.id,p.title]));
    const quote=v=>'"'+String(v??'').replace(/"/g,'""')+'"';
    const rows=[['Дата и время','Название','ID','Просмотры','Лайки','Комментарии','Репосты']];
    for(const [id,t,...values] of d.raw){let name=titles.get(id)||'';if(/^[\s]*[=+@-]/.test(name))name="'"+name;rows.push([t,name,id,...values]);}
    const url=URL.createObjectURL(new Blob(['\ufeff',rows.map(r=>r.map(quote).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'}));
    const a=document.createElement('a');a.href=url;a.download='tach_'+d.profile+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),60000);
  }
  return {merge,summarize,csv};
})();
