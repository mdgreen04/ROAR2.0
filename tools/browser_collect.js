// ROAR 2.0 — in-browser collector (offline fallback)
//
// Use this when the machine that runs ROAR cannot reach PubMed or the feeds (e.g. a sandbox) but a normal
// browser can. It reproduces `roar fetch` stage 1 + 2 for PubMed inside the browser and saves the results
// as JSON downloads that tools/items_from_browser_export.py turns into item files.
//
// How to use
//   1. Open https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi?db=pubmed&retmode=json in a browser tab
//      (same origin as the E-utilities, so fetch() is not blocked by CORS).
//   2. Paste the QUERIES array printed by:  python -c "from roar.util import load_config; from roar.fetch_pubmed import expand_query; import json; c=load_config(); f=c['pubmed']['fragments']; print(json.dumps([{'id':q['id'],'tier':q['tier'],'topic':q['topic'],'term':expand_query(q['term'],f)} for q in c['pubmed']['queries']]))"
//      into the QUERIES constant below, then paste the whole file into the browser console.
//   3. Run  await roarStage1()      -> downloads roar_pubmed_stage1.json (titles/journals/ids for every hit)
//   4. Pre-score in Python to choose PMIDs (see README), then run  await roarStage2([...pmids...])
//      -> downloads roar_pubmed_abstracts.json
//   5. For a feed, open its URL in a tab and run  roarFeed('feed_id')  -> downloads roar_rss_<feed_id>.json
//   6. python tools/items_from_browser_export.py --stage1 ... --abstracts ... --rss "roar_rss*.json" --out digests/_offline
//      python -m roar fetch --from-items digests/_offline/items_pubmed.json digests/_offline/items_rss.json

const QUERIES = []; // <- paste here

const EUTILS = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/';
const IDENT = 'tool=ROAR2.0&email=you@gmail.com';

function roarDownload(name, obj) {
  const blob = new Blob([JSON.stringify(obj)], { type: 'application/json' });
  const a = document.createElementNS('http://www.w3.org/1999/xhtml', 'a');
  a.setAttribute('href', URL.createObjectURL(blob));
  a.setAttribute('download', name);
  (document.body || document.documentElement).appendChild(a);
  a.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
}

async function roarStage1(lookbackDays = 8, retmax = 400) {
  const hits = {}, report = [];
  for (const q of QUERIES) {
    const u = `${EUTILS}esearch.fcgi?db=pubmed&retmode=json&retmax=${retmax}&sort=date&reldate=${lookbackDays}&datetype=edat&${IDENT}&term=${encodeURIComponent(q.term)}`;
    try {
      const j = await (await fetch(u)).json();
      const ids = (j.esearchresult && j.esearchresult.idlist) || [];
      report.push([q.id, ids.length]);
      for (const id of ids) {
        const h = hits[id] || (hits[id] = { tags: [], tier: 9, topics: [] });
        h.tags.push(q.id); h.tier = Math.min(h.tier, q.tier);
        if (!h.topics.includes(q.topic)) h.topics.push(q.topic);
      }
    } catch (e) { report.push([q.id, 'ERR', String(e).slice(0, 80)]); }
  }
  const pmids = Object.keys(hits), recs = [];
  for (let i = 0; i < pmids.length; i += 200) {
    const chunk = pmids.slice(i, i + 200);
    const r = await fetch(`${EUTILS}esummary.fcgi`, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: `db=pubmed&retmode=json&${IDENT}&id=${chunk.join(',')}` });
    const res = (await r.json()).result || {};
    for (const id of chunk) {
      const s = res[id]; if (!s || s.error) continue;
      let doi = ''; for (const a of (s.articleids || [])) if (a.idtype === 'doi') doi = a.value;
      recs.push({ p: id, t: s.title || '', j: s.source || '', d: s.epubdate || s.sortpubdate || s.pubdate || '',
        o: doi, y: (s.pubtype || []).slice(0, 3), q: hits[id].tags, r: hits[id].tier, k: hits[id].topics });
    }
  }
  roarDownload('roar_pubmed_stage1.json', { report, recs });
  return { report, n: recs.length };
}

async function roarStage2(pmids, maxChars = 3200) {
  const out = [];
  for (let i = 0; i < pmids.length; i += 100) {
    const chunk = pmids.slice(i, i + 100);
    const r = await fetch(`${EUTILS}efetch.fcgi`, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: `db=pubmed&retmode=xml&rettype=abstract&${IDENT}&id=${chunk.join(',')}` });
    const doc = new DOMParser().parseFromString(await r.text(), 'text/xml');
    for (const art of doc.querySelectorAll('PubmedArticle')) {
      const pm = art.querySelector('MedlineCitation > PMID'); if (!pm) continue;
      const parts = [];
      for (const at of art.querySelectorAll('Abstract > AbstractText')) {
        const lab = at.getAttribute('Label'); const t = at.textContent.trim(); if (!t) continue;
        parts.push(lab && lab.toUpperCase() !== 'UNLABELLED' ? lab.charAt(0) + lab.slice(1).toLowerCase() + ': ' + t : t);
      }
      const y = [...art.querySelectorAll('PublicationTypeList > PublicationType')].map(e => e.textContent.trim());
      let o = ''; for (const e of art.querySelectorAll('ELocationID')) if (e.getAttribute('EIdType') === 'doi') o = e.textContent.trim();
      if (!o) for (const e of art.querySelectorAll('ArticleIdList > ArticleId')) if (e.getAttribute('IdType') === 'doi') o = e.textContent.trim();
      let d = ''; const ad = art.querySelector('ArticleDate');
      if (ad) d = ['Year', 'Month', 'Day'].map(k => (ad.querySelector(k) || {}).textContent || '').join('-');
      out.push({ p: pm.textContent.trim(), a: parts.join(' ').replace(/\s+/g, ' ').slice(0, maxChars), y, o, d });
    }
  }
  roarDownload('roar_pubmed_abstracts.json', out);
  return out.length;
}

// Run on a tab that is showing the feed XML itself.
function roarFeed(feedId) {
  const xml = (document.body && document.body.innerText.startsWith('<')) ? document.body.innerText
    : new XMLSerializer().serializeToString(document);
  const doc = new DOMParser().parseFromString(xml, 'text/xml');
  const g = (el, names) => { for (const n of names) { const c = [...el.children].find(x => x.localName === n || x.nodeName === n); if (c) return c; } return null; };
  const strip = h => h.replace(/<!\[CDATA\[|\]\]>/g, '').replace(/<[^>]+>/g, ' ').replace(/&amp;/g, '&').replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>').replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&nbsp;/g, ' ');
  const recs = [...doc.querySelectorAll('item, entry')].map(it => {
    const t = g(it, ['title']), l = g(it, ['link']), d = g(it, ['pubDate', 'published', 'updated', 'date', 'dc:date']);
    const s = g(it, ['description', 'summary', 'content', 'encoded']), id = g(it, ['identifier', 'dc:identifier']);
    return { t: t ? strip(t.textContent).trim() : '', l: l ? (l.getAttribute('href') || l.textContent).trim() : '',
      d: d ? d.textContent.trim() : '', s: s ? strip(s.textContent).replace(/\s+/g, ' ').trim().slice(0, 700) : '',
      i: id ? id.textContent.trim() : '', src: feedId };
  });
  roarDownload(`roar_rss_${feedId}.json`, recs);
  return recs.length;
}
