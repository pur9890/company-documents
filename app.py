"""
Filings Desk — download annual reports, concall transcripts, investor
presentations and credit rating reports for any listed Indian company,
straight from Screener.in.

Run:  python app.py      (then it opens http://127.0.0.1:5000 in your browser)
"""
import os
import re
import tempfile
import threading
import uuid
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse, quote

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, abort, jsonify, request, send_file

BASE = "https://www.screener.in"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
_nse_warm = False

KINDS = {
    "annual":       "Annual Reports",
    "concall":      "Concall Transcripts",
    "presentation": "Investor Presentations",
    "rating":       "Credit Ratings",
    "quarterly":    "Quarterly Results",
}
MONTHS = {3: "Mar", 6: "Jun", 9: "Sep", 12: "Dec"}
OLDEST_QUARTER_YEAR = 2010   # older quarters are tried back to this year

app = Flask(__name__)


# ----------------------------------------------------------------- Screener
def search_companies(q):
    r = session.get(f"{BASE}/api/company/search/",
                    params={"q": q, "v": 3, "fts": 1}, timeout=20)
    r.raise_for_status()
    out = []
    for d in r.json():
        url = d.get("url", "")
        if url.startswith("/company/"):
            out.append({"name": d.get("name", ""), "url": url})
    return out[:10]


def _section(soup, css, heading):
    sec = soup.select_one(css)
    if sec:
        return sec
    for h in soup.find_all(["h2", "h3"]):
        if h.get_text(" ", strip=True).lower() == heading.lower():
            return h.find_parent("div")
    return None


def _clean(t):
    return " ".join((t or "").split())


def _year(text):
    m = re.search(r"\b(19|20)\d{2}\b", text or "")
    return int(m.group(0)) if m else None


def get_documents(company_url):
    if not company_url.startswith("/company/"):
        raise ValueError("Not a Screener company URL")
    r = session.get(urljoin(BASE, company_url), timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    title = soup.find("h1")
    name = _clean(title.get_text()) if title else company_url
    code = company_url.strip("/").split("/")[1]

    items, seen = [], set()

    def add(kind, url, label, period, year, source=""):
        url = urljoin(BASE, url.strip())
        if not url.startswith("http") or (kind, url) in seen:
            return
        seen.add((kind, url))
        items.append({"kind": kind, "url": url, "label": label,
                      "period": period, "year": year, "source": source})

    # Annual reports
    sec = _section(soup, "div.documents.annual-reports", "Annual reports")
    if sec:
        for a in sec.find_all("a", href=True):
            text = _clean(a.get_text(" "))
            y = _year(text)
            if y is None:
                continue
            li = a.find_parent("li")
            full = _clean(li.get_text(" ")) if li else text
            src = re.search(r"from\s+(\w+)", full)
            add("annual", a["href"], f"Annual Report {y}", str(y), y,
                src.group(1).upper() if src else "")

    # Credit ratings
    sec = _section(soup, "div.documents.credit-ratings", "Credit ratings")
    if sec:
        for a in sec.find_all("a", href=True):
            text = _clean(a.get_text(" "))
            d = re.search(r"(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})", text)
            ag = re.search(r"from\s+([A-Za-z]+)", text)
            date = d.group(1) if d else ""
            agency = ag.group(1).upper() if ag else ""
            if agency == "FITCH":
                agency = "INDIA RATINGS"
            add("rating", a["href"], f"{agency or 'Rating'} rating update",
                date, _year(date), agency)

    # Concalls: transcripts + presentations
    sec = _section(soup, "div.documents.concalls", "Concalls")
    if sec:
        for li in sec.find_all("li"):
            text = _clean(li.get_text(" "))
            m = re.search(r"\b([A-Z][a-z]{2})[a-z]*\s+((?:19|20)\d{2})\b", text)
            period = f"{m.group(1)} {m.group(2)}" if m else "Undated"
            y = int(m.group(2)) if m else None
            for a in li.find_all("a", href=True):
                lab = a.get_text(strip=True).lower()
                if lab.startswith("transcript"):
                    add("concall", a["href"], f"Concall transcript {period}", period, y)
                elif lab == "ppt":
                    add("presentation", a["href"], f"Investor presentation {period}", period, y)

    # Quarterly results (Screener's "Raw PDF" row). Screener only shows the
    # last ~13 quarters, but the same link pattern works for older ones, so
    # we also list older quarters back to OLDEST_QUARTER_YEAR.
    qlinks = soup.find_all("a", href=re.compile(r"/company/source/quarter/\d+/\d+/\d{4}"))
    shown = set()
    cid = None
    for a in qlinks:
        m = re.search(r"/company/source/quarter/(\d+)/(\d+)/(\d{4})", a["href"])
        cid, mo, yr = m.group(1), int(m.group(2)), int(m.group(3))
        shown.add((yr, mo))
    if cid:
        latest = max(shown)
        yr, mo = latest
        while yr >= OLDEST_QUARTER_YEAR:
            if mo in MONTHS:
                fy = yr if mo <= 3 else yr + 1
                qn = {6: 1, 9: 2, 12: 3, 3: 4}[mo]
                period = f"Q{qn} FY{str(fy)[2:]} ({MONTHS[mo]} {yr})"
                add("quarterly", f"/company/source/quarter/{cid}/{mo}/{yr}/",
                    f"Quarterly result Q{qn} FY{str(fy)[2:]}", period, yr,
                    "" if (yr, mo) in shown else "older")
            mo -= 3
            if mo <= 0:
                mo += 12
                yr -= 1

    return {"name": name, "code": code, "url": urljoin(BASE, company_url), "items": items}


# ----------------------------------------------------------------- Download
def fetch_file(url):
    global _nse_warm
    host = urlparse(url).netloc.lower()
    headers = {"Accept": "application/pdf,text/html,*/*"}
    if "bseindia" in host or "screener" in host:
        headers["Referer"] = "https://www.bseindia.com/"
    if "nseindia" in host:
        headers["Referer"] = "https://www.nseindia.com/"
        if not _nse_warm:
            try:
                session.get("https://www.nseindia.com/", timeout=15)
            except requests.RequestException:
                pass
            _nse_warm = True
    r = session.get(url, headers=headers, timeout=120, allow_redirects=True)
    r.raise_for_status()
    return r.content, r.headers.get("Content-Type", "").lower()


def extension(url, ctype, content):
    if content[:5] == b"%PDF-" or "pdf" in ctype:
        return ".pdf"
    if "html" in ctype:
        return ".html"
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    return ext if ext in (".pdf", ".html", ".htm", ".ppt", ".pptx", ".doc", ".docx") else ".pdf"


def safe(s):
    s = re.sub(r"[^\w\-. ]+", "", s or "").strip().replace(" ", "_")
    return s[:120] or "file"


def base_name(code, item):
    tag = {"annual": "AnnualReport", "concall": "ConcallTranscript",
           "presentation": "InvestorPPT", "rating": "CreditRating",
           "quarterly": "QuarterlyResult"}[item["kind"]]
    parts = [code, tag]
    if item["kind"] == "rating" and item.get("source"):
        parts.append(item["source"])
    parts.append((item.get("period") or "").replace("(", "").replace(")", ""))
    return safe("_".join(p for p in parts if p))


JOBS = {}


def run_zip_job(job_id, code, items):
    job = JOBS[job_id]
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    used, failed = set(), []

    def work(it):
        try:
            content, ctype = fetch_file(it["url"])
            if it["kind"] == "quarterly" and content[:5] != b"%PDF-":
                raise ValueError("not available for this quarter")
            return it, content, ctype, None
        except Exception as e:  # noqa: BLE001
            return it, None, None, str(e)

    try:
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as z, \
                ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(work, it) for it in items]
            for f in as_completed(futures):
                it, content, ctype, err = f.result()
                if err:
                    failed.append(f"{it['label']}  —  {it['url']}  ({err})")
                else:
                    folder = KINDS[it["kind"]]
                    name = base_name(code, it) + extension(it["url"], ctype, content)
                    path, n = f"{folder}/{name}", 2
                    while path in used:
                        stem, ext = os.path.splitext(name)
                        path = f"{folder}/{stem}_{n}{ext}"
                        n += 1
                    used.add(path)
                    z.writestr(path, content)
                job["done"] += 1
                job["failed"] = len(failed)
            if failed:
                z.writestr("_could_not_download.txt", "\n".join(failed))
        job["path"] = tmp.name
        job["status"] = "ready"
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(e)


# ----------------------------------------------------------------- Routes
@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.get("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])
    try:
        return jsonify(search_companies(q))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"Screener search failed: {e}"}), 502


@app.get("/api/docs")
def api_docs():
    try:
        return jsonify(get_documents(request.args.get("url", "")))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"Could not read the Screener page: {e}"}), 502


@app.get("/api/file")
def api_file():
    url = request.args.get("url", "")
    name = request.args.get("name", "document")
    kind = request.args.get("kind", "")
    if urlparse(url).scheme not in ("http", "https"):
        abort(400)
    try:
        content, ctype = fetch_file(url)
    except Exception as e:  # noqa: BLE001
        return Response(f"Download failed: {e}\n\nOpen the original instead: {url}",
                        status=502, mimetype="text/plain")
    if kind == "quarterly" and content[:5] != b"%PDF-":
        return Response("This quarter's result PDF is not available on Screener.",
                        status=404, mimetype="text/plain")
    ext = extension(url, ctype, content)
    fname = safe(name) + ext
    mime = "application/pdf" if ext == ".pdf" else (ctype or "application/octet-stream")
    return Response(content, mimetype=mime, headers={
        "Content-Disposition": f"attachment; filename=\"{fname}\"; filename*=UTF-8''{quote(fname)}"})


@app.post("/api/zip")
def api_zip():
    body = request.get_json(force=True)
    items = [i for i in body.get("items", []) if i.get("kind") in KINDS]
    if not items:
        return jsonify({"error": "Nothing selected"}), 400
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "running", "done": 0, "total": len(items), "failed": 0,
                    "code": safe(body.get("code", "company"))}
    threading.Thread(target=run_zip_job, args=(job_id, JOBS[job_id]["code"], items),
                     daemon=True).start()
    return jsonify({"job": job_id})


@app.get("/api/zip/<job_id>")
def api_zip_status(job_id):
    job = JOBS.get(job_id) or abort(404)
    return jsonify({k: v for k, v in job.items() if k != "path"})


@app.get("/api/zip/<job_id>/file")
def api_zip_file(job_id):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "ready":
        abort(404)
    return send_file(job["path"], as_attachment=True,
                     download_name=f"{job['code']}_documents.zip")


# ----------------------------------------------------------------- Page
PAGE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Filings Desk</title>
<link href="https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#EEF1F4;--paper:#fff;--ink:#16263A;--muted:#5B6878;--line:#D3DAE2;--accent:#1D5FA8;--stamp:#B3261E;--ok:#1E7B4F;
  --sans:"Schibsted Grotesk","Segoe UI",system-ui,Arial,sans-serif}
@media (prefers-color-scheme:dark){:root{--bg:#0F1722;--paper:#162232;--ink:#E6ECF3;--muted:#9AA8B8;--line:#2A3A4E;--accent:#6FA8E8;--stamp:#F08A80;--ok:#6CCB9A}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 var(--sans)}
.wrap{max-width:980px;margin:0 auto;padding:36px 20px 80px}
.brand{font-weight:800}.brand span{color:var(--stamp)}
h1{font-size:clamp(1.9rem,5vw,3rem);line-height:1.05;letter-spacing:-.03em;margin:36px 0 22px;max-width:18ch}
.searchbox{position:relative;max-width:680px}
.searchbox input{width:100%;font:600 1.2rem var(--sans);color:var(--ink);background:var(--paper);border:2px solid var(--ink);border-radius:10px;padding:16px 18px}
.searchbox input:focus{outline:none;box-shadow:0 0 0 4px color-mix(in srgb,var(--accent) 30%,transparent)}
.sugg{position:absolute;left:0;right:0;top:calc(100% + 6px);background:var(--paper);border:1px solid var(--line);border-radius:10px;list-style:none;margin:0;padding:6px;z-index:5;box-shadow:0 12px 30px rgba(20,30,50,.15)}
.sugg li{padding:10px 12px;border-radius:6px;cursor:pointer}.sugg li[aria-selected=true],.sugg li:hover{background:var(--bg)}
.status{margin-top:14px;color:var(--muted);min-height:1.5em}.status.err{color:var(--stamp)}
.company{margin-top:30px;display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
.company h2{margin:0;font-size:1.7rem;letter-spacing:-.02em}.company a{color:var(--accent);font-weight:600}
.filters{margin-top:18px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.chip{display:flex;align-items:center;gap:8px;border:1.5px solid var(--line);background:var(--paper);border-radius:999px;padding:8px 14px;cursor:pointer;font-weight:600;user-select:none}
.chip input{accent-color:var(--accent);width:17px;height:17px}.chip:has(input:checked){border-color:var(--accent)}
.chip .n{color:var(--muted);font-weight:500}
.filters select{font:600 .95rem var(--sans);padding:8px 10px;border-radius:8px;border:1.5px solid var(--line);background:var(--paper);color:var(--ink)}
.bar{margin-top:18px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.btn{border:0;background:var(--ink);color:var(--bg);font:700 1rem var(--sans);padding:13px 22px;border-radius:10px;cursor:pointer}
.btn:disabled{opacity:.5;cursor:not-allowed}
.progress{flex:1;min-width:200px;color:var(--muted);font-size:.95rem}
.track{height:8px;background:var(--line);border-radius:99px;overflow:hidden;margin-top:6px}.fill{height:100%;background:var(--ok);width:0;transition:width .3s}
.group{margin-top:28px;background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:6px 20px 8px;border-top:5px solid var(--ink)}
.group h3{margin:14px 0 6px;font-size:1.15rem}
.row{display:flex;align-items:center;gap:12px;padding:10px 0;border-top:1px solid var(--line)}
.row:first-of-type{border-top:0}
.row .t{flex:1;font-weight:600}.row .s{color:var(--muted);font-size:.85rem;font-weight:500;margin-left:8px}
.row a{font-weight:600;text-decoration:none;color:var(--accent);white-space:nowrap}
.row a.dl{border:1.5px solid var(--accent);border-radius:8px;padding:5px 12px}
.empty{margin-top:36px;padding:24px;border:2px dashed var(--line);border-radius:14px;color:var(--muted);max-width:680px}
</style></head><body><div class="wrap">
<div class="brand">Filings<span>.</span>Desk</div>
<h1>Type a symbol. Download every filing.</h1>
<div class="searchbox">
  <input id="q" placeholder="Company symbol, e.g. POLYCAB, KPITTECH, TATAMOTORS" autocomplete="off" aria-label="Company name">
  <ul class="sugg" id="sugg" hidden></ul>
</div>
<div class="status" id="status"></div>
<div id="out"><div class="empty">Pick a company from the suggestions. You'll then choose which documents you want and download them one by one or all together as a ZIP.</div></div>
</div>
<script>
const KINDS={annual:"Annual Reports",concall:"Concall Transcripts",presentation:"Investor Presentations",rating:"Credit Ratings",quarterly:"Quarterly Results"};
const $=s=>document.querySelector(s);
let docs=null, sel=-1, list=[], timer;
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function status(t,err){const s=$("#status");s.textContent=t||"";s.className="status"+(err?" err":"");}

$("#q").addEventListener("input",()=>{clearTimeout(timer);const q=$("#q").value.trim();if(q.length<2){hideSugg();return;}timer=setTimeout(()=>suggest(q),250);});
$("#q").addEventListener("keydown",e=>{
  if($("#sugg").hidden)return;
  if(e.key==="ArrowDown"){sel=Math.min(sel+1,list.length-1);drawSugg();e.preventDefault();}
  else if(e.key==="ArrowUp"){sel=Math.max(sel-1,0);drawSugg();e.preventDefault();}
  else if(e.key==="Enter"){e.preventDefault();pick(list[Math.max(sel,0)]);}
  else if(e.key==="Escape")hideSugg();
});
document.addEventListener("click",e=>{if(!e.target.closest(".searchbox"))hideSugg();});
function hideSugg(){$("#sugg").hidden=true;}
async function suggest(q){
  try{const r=await fetch("/api/search?q="+encodeURIComponent(q));const d=await r.json();
    if(d.error){status(d.error,true);return;}
    list=d;sel=list.length?0:-1;drawSugg();
    if(!list.length)status("No listed company matches that name on Screener.",true);else status("");
  }catch(e){status("Search failed. Check your internet connection.",true);}
}
function drawSugg(){const u=$("#sugg");if(!list.length){u.hidden=true;return;}
  u.innerHTML=list.map((c,i)=>`<li role="option" aria-selected="${i===sel}" data-i="${i}">${esc(c.name)}</li>`).join("");
  u.hidden=false;u.querySelectorAll("li").forEach(li=>li.onclick=()=>pick(list[+li.dataset.i]));}

async function pick(c){
  if(!c)return;hideSugg();$("#q").value=c.name;status("Reading "+c.name+" documents from Screener…");$("#out").innerHTML="";
  try{const r=await fetch("/api/docs?url="+encodeURIComponent(c.url));docs=await r.json();
    if(docs.error){status(docs.error,true);return;}
    status(docs.items.length?"":"Screener lists no documents for this company.",!docs.items.length);render();
  }catch(e){status("Could not reach Screener. Try again in a moment.",true);}
}

function chosen(){
  const kinds=[...document.querySelectorAll(".chip input:checked")].map(i=>i.value);
  const from=+($("#from")?.value||0);
  return docs.items.filter(it=>kinds.includes(it.kind)&&(!from||!it.year||it.year>=from));
}
function fname(it){const tag={annual:"AnnualReport",concall:"ConcallTranscript",presentation:"InvestorPPT",rating:"CreditRating",quarterly:"QuarterlyResult"}[it.kind];
  return [docs.code,tag,it.kind==="rating"?it.source:"",String(it.period).replace(/[()]/g,"")].filter(Boolean).join("_");}

function render(){
  const counts={};docs.items.forEach(i=>counts[i.kind]=(counts[i.kind]||0)+1);
  const years=[...new Set(docs.items.map(i=>i.year).filter(Boolean))].sort((a,b)=>b-a);
  $("#out").innerHTML=`
   <div class="company"><h2>${esc(docs.name)}</h2><a href="${esc(docs.url)}" target="_blank" rel="noopener">View on Screener</a></div>
   <div class="filters">
     ${Object.entries(KINDS).map(([k,v])=>`<label class="chip"><input type="checkbox" value="${k}" ${counts[k]?"checked":"disabled"}> ${v} <span class="n">${counts[k]||0}</span></label>`).join("")}
     <select id="from" aria-label="From year"><option value="0">All years</option>${years.map(y=>`<option value="${y}">From ${y}</option>`).join("")}</select>
   </div>
   <div class="bar"><button class="btn" id="zip">Download selected as ZIP</button><div class="progress" id="prog"></div></div>
   <div id="groups"></div>`;
  document.querySelectorAll(".chip input,#from").forEach(el=>el.onchange=drawGroups);
  $("#zip").onclick=zipAll;drawGroups();
}
function drawGroups(){
  const items=chosen();$("#zip").textContent=`Download selected as ZIP (${items.length})`;$("#zip").disabled=!items.length;
  $("#groups").innerHTML=Object.entries(KINDS).map(([k,v])=>{
    const rows=items.filter(i=>i.kind===k);if(!rows.length)return"";
    return `<section class="group"><h3>${v}</h3>${rows.map(it=>`
      <div class="row"><span class="t">${esc(it.label)}${it.kind==="quarterly"?`<span class="s">${esc(it.period.replace(/^.*\(|\)$/g,""))}${it.source==="older"?" · older, if available":""}</span>`:""}${it.source&&it.kind!=="rating"&&it.kind!=="quarterly"?`<span class="s">from ${esc(it.source)}</span>`:""}${it.kind==="rating"&&it.period?`<span class="s">${esc(it.period)}</span>`:""}</span>
      <a href="${esc(it.url)}" target="_blank" rel="noopener">Open</a>
      <a class="dl" href="/api/file?url=${encodeURIComponent(it.url)}&name=${encodeURIComponent(fname(it))}&kind=${it.kind}">Download</a></div>`).join("")}</section>`;}).join("")
    || `<div class="empty">No documents match these filters.</div>`;
}
async function zipAll(){
  const items=chosen();if(!items.length)return;const btn=$("#zip");btn.disabled=true;
  const prog=$("#prog");prog.innerHTML=`Starting… <div class="track"><div class="fill"></div></div>`;
  try{
    const r=await fetch("/api/zip",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({code:docs.code,items})});
    const {job,error}=await r.json();if(error)throw new Error(error);
    const tick=async()=>{
      const s=await (await fetch("/api/zip/"+job)).json();
      prog.innerHTML=`Downloaded ${s.done} of ${s.total}${s.failed?` · ${s.failed} failed`:""}<div class="track"><div class="fill" style="width:${100*s.done/s.total}%"></div></div>`;
      if(s.status==="running")return setTimeout(tick,800);
      btn.disabled=false;
      if(s.status==="ready"){window.location="/api/zip/"+job+"/file";
        prog.innerHTML=`ZIP ready — ${s.total-s.failed} files saved to your Downloads.${s.failed?" See _could_not_download.txt inside for the rest.":""}`;}
      else prog.textContent="ZIP failed: "+(s.error||"unknown error");
    };tick();
  }catch(e){prog.textContent="ZIP failed: "+e.message;btn.disabled=false;}
}
$("#q").focus();
</script></body></html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    print(f"\n  Filings Desk is running at http://127.0.0.1:{port}  (close this window to stop)\n")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
