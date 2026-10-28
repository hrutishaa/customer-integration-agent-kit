const CRM=[{id:"c_101",full_name:"Maya Chen",email_address:"MAYA@trailhead.io",company:"Trailhead",lifecycle:"customer",updated:"2026-10-20"},
{id:"c_102",full_name:"Luis Ortega",email_address:"luis@ridgeline.co",company:"Ridgeline",lifecycle:"lead",updated:"2026-10-22"},
{id:"c_103",full_name:"Priya Rao",email_address:"priya@summit.dev",company:"Summit",lifecycle:"opportunity",updated:"2026-10-25"},
{id:"c_104",full_name:"Sam Okafor",email_address:"sam@basecamp.net",company:"Basecamp",lifecycle:"customer",updated:"2026-10-26"}];
const CSV=`Contact ID,Name,E-mail,Account,Stage,Phone
csv_9,Jordan Lee,jordan@pinecrest.com,Pinecrest,Lead,555-0101
csv_10,Maya Chen,maya@trailhead.io,Trailhead,Customer,555-0102
csv_11,Ana Silva,ana@harbor.io,Harbor,Opportunity,555-0103`;
const MAP=[["full_name","Name","name"],["email_address","E-mail","email (lower-cased, dedupe key)"],["company","Account","account"],["lifecycle","Stage","stage (normalized)"],["—","Phone","phone"]];
const $=id=>document.getElementById(id);let store=new Map(),seen=new Set(),runs=0;
$("crm").textContent=JSON.stringify(CRM,null,1);$("csv").textContent=CSV;
$("map").innerHTML="<tr><th>CRM</th><th>CSV</th><th>Target</th></tr>"+MAP.map(r=>`<tr>${r.map(c=>`<td>${c}</td>`).join("")}</tr>`).join("");
$("fail").oninput=e=>$("failv").textContent=e.target.value+"%";
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function log(msg,c="dim"){const s=document.createElement("span");s.className=c;s.textContent=msg+"\n";$("log").appendChild(s);$("log").scrollTop=1e9}
async function mockGet(page){const p=+$("fail").value/100,r=Math.random();
 if(r<p/2)return{status:429,headers:{"Retry-After":"1"}};if(r<p)return{status:[500,502,503][page%3]};
 return{status:200,body:CRM.slice(page*2,page*2+2),next:page*2+2<CRM.length?page+1:null}}
async function restGet(page,max=5){for(let a=1;a<=max;a++){const res=await mockGet(page);
 if(res.status===200){log(`  GET /contacts?page=${page} → 200 (${res.body.length} records)`,"ok");return res}
 const wait=res.headers?.["Retry-After"]?+res.headers["Retry-After"]*1000:Math.min(2000,200*2**(a-1));
 log(`  GET /contacts?page=${page} → ${res.status}; retry ${a}/${max} in ${wait}ms${res.status===429?" (honoring Retry-After)":" (exp. backoff)"}`,"warn");await sleep(wait/4)}
 throw new Error("retries exhausted")}
const stage=s=>s.toLowerCase();
function upsert(rec,key){const k=rec.email;const ex=store.get(k);
 if(seen.has(key)){log(`  ⤳ skip ${key}: idempotency key already applied`,"dim");return"skip"}seen.add(key);
 if(ex){Object.assign(ex,Object.fromEntries(Object.entries(rec).filter(([,v])=>v)));ex.sources=[...new Set([...ex.sources,...rec.sources])];log(`  ✎ merged ${k} (${ex.sources.join(" + ")})`,"ok");return"merge"}
 store.set(k,{...rec});log(`  + created ${k}`,"ok");return"create"}
async function run(){$("run").disabled=$("again").disabled=true;runs++;const t=Date.now();const st={create:0,merge:0,skip:0};
 log(`\n▶ Run #${runs} · agent plan: fetch_crm → read_csv → map_schema → upsert_records`,"");
 try{log("tool fetch_crm()","");let page=0;while(page!==null){const r=await restGet(page);
  for(const c of r.body){const rec={name:c.full_name,email:c.email_address.toLowerCase(),account:c.company,stage:stage(c.lifecycle),phone:"",sources:["crm"]};
   st[upsert(rec,`crm:${c.id}:${c.updated}`)]++;if($("dup").checked&&c.id==="c_102"){log("  ↺ duplicate delivery of c_102 received","warn");st[upsert(rec,`crm:${c.id}:${c.updated}`)]++}}page=r.next}
  log("tool read_csv('acme_contacts.csv')","");const [hdr,...rows]=CSV.split("\n").map(l=>l.split(","));
  const allowed=["Contact ID","Name","E-mail","Account","Stage","Phone"];if(hdr.some(h=>!allowed.includes(h)))throw new Error("unknown column");
  log(`  columns validated against allow-list (${hdr.length})`,"ok");
  for(const r of rows){const o=Object.fromEntries(hdr.map((h,i)=>[h,r[i]]));
   st[upsert({name:o.Name,email:o["E-mail"].toLowerCase(),account:o.Account,stage:stage(o.Stage),phone:o.Phone,sources:["csv"]},`csv:${o["Contact ID"]}`)]++}
  log(`✔ done in ${((Date.now()-t)/1000).toFixed(1)}s`,"ok")}catch(e){log("✖ "+e.message+" — sync halted safely, no partial duplicates","err")}
 $("stats").innerHTML=`<span class="ok">created ${st.create}</span><span class="ok">merged ${st.merge}</span><span class="dim">skipped (idempotent) ${st.skip}</span><span>total records ${store.size}</span>`;
 $("out").innerHTML="<tr><th>email</th><th>name</th><th>account</th><th>stage</th><th>phone</th><th>sources</th></tr>"+[...store.values()].map(r=>`<tr><td>${r.email}</td><td>${r.name}</td><td>${r.account}</td><td>${r.stage}</td><td>${r.phone||"—"}</td><td>${r.sources.join(", ")}</td></tr>`).join("");
 $("run").disabled=$("again").disabled=false}
$("run").onclick=run;$("again").onclick=run;$("reset").onclick=()=>{store.clear();seen.clear();runs=0;$("log").textContent="";$("out").innerHTML="";$("stats").innerHTML=""};
