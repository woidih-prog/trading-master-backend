<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TM V5 — Admin Dashboard</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;700;800&display=swap');

:root{
  --gold:#FFD700;--silver:#C0C0C0;--green:#00C853;--red:#ff4444;--orange:#FFA500;
  --bg:#050a05;--bg2:#0a120a;--bg3:#0d180d;--border:#1a2e1a;
  --text:#e8e8e8;--muted:#4a6a4a;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Syne',sans-serif;min-height:100vh;
  background-image:radial-gradient(ellipse 80% 50% at 50% -10%,#0d2a0d,transparent),
  repeating-linear-gradient(0deg,transparent,transparent 40px,#0d180d22 40px,#0d180d22 41px),
  repeating-linear-gradient(90deg,transparent,transparent 40px,#0d180d22 40px,#0d180d22 41px);}

/* HEADER */
.header{padding:24px 32px;display:flex;align-items:center;justify-content:space-between;
  border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100;
  background:rgba(5,10,5,0.95);backdrop-filter:blur(12px);}
.logo-area{display:flex;align-items:center;gap:14px}
.logo-icon{width:44px;height:44px;background:linear-gradient(135deg,var(--gold),var(--orange));
  border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:20px;
  box-shadow:0 0 20px #FFD70040}
.logo-text{font-size:11px;font-weight:800;letter-spacing:4px;color:var(--gold);font-family:'Space Mono',monospace}
.logo-sub{font-size:8px;color:var(--muted);letter-spacing:3px;margin-top:2px}
.header-right{display:flex;align-items:center;gap:12px}
.refresh-btn{background:var(--bg3);border:1px solid var(--border);color:var(--gold);
  border-radius:8px;padding:8px 16px;font-size:10px;font-family:'Space Mono',monospace;
  font-weight:700;cursor:pointer;letter-spacing:1px;transition:all .2s}
.refresh-btn:hover{background:#FFD70015;border-color:var(--gold)}
.last-update{font-size:9px;color:var(--muted);font-family:'Space Mono',monospace}

.container{max-width:1200px;margin:0 auto;padding:32px 24px}

/* SECTION TITLE */
.section-title{font-size:9px;font-weight:700;letter-spacing:4px;color:var(--muted);
  font-family:'Space Mono',monospace;margin-bottom:16px;text-transform:uppercase}

/* KPI GRID */
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:32px}
.kpi{background:var(--bg3);border:1px solid var(--border);border-radius:14px;padding:18px 20px;
  position:relative;overflow:hidden;transition:border-color .2s}
.kpi::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;border-radius:2px 2px 0 0}
.kpi.gold::before{background:linear-gradient(90deg,transparent,var(--gold),transparent)}
.kpi.green::before{background:linear-gradient(90deg,transparent,var(--green),transparent)}
.kpi.red::before{background:linear-gradient(90deg,transparent,var(--red),transparent)}
.kpi.silver::before{background:linear-gradient(90deg,transparent,var(--silver),transparent)}
.kpi.orange::before{background:linear-gradient(90deg,transparent,var(--orange),transparent)}
.kpi-label{font-size:8px;color:var(--muted);letter-spacing:2px;font-family:'Space Mono',monospace;margin-bottom:8px}
.kpi-value{font-size:32px;font-weight:800;font-family:'Space Mono',monospace;line-height:1}
.kpi-sub{font-size:9px;color:var(--muted);margin-top:6px;font-family:'Space Mono',monospace}

/* GRID 2 COL */
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-bottom:24px}
@media(max-width:700px){.grid2,.grid3{grid-template-columns:1fr}}

/* CARD */
.card{background:var(--bg3);border:1px solid var(--border);border-radius:14px;padding:20px 22px}
.card-title{font-size:9px;font-weight:700;letter-spacing:3px;color:var(--gold);
  font-family:'Space Mono',monospace;margin-bottom:16px;text-transform:uppercase}

/* PROGRESS BAR */
.prog-row{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.prog-label{font-size:10px;color:var(--text);font-family:'Space Mono',monospace;min-width:90px}
.prog-bar{flex:1;height:8px;background:#0a120a;border-radius:4px;overflow:hidden}
.prog-fill{height:100%;border-radius:4px;transition:width 1s ease}
.prog-val{font-size:10px;font-weight:700;font-family:'Space Mono',monospace;min-width:40px;text-align:right}
.prog-count{font-size:9px;color:var(--muted);font-family:'Space Mono',monospace;min-width:30px;text-align:right}

/* MATRICE */
.matrix{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-top:8px}
.matrix-header{font-size:8px;color:var(--muted);font-family:'Space Mono',monospace;
  text-align:center;padding:6px 4px;letter-spacing:1px}
.matrix-cell{border-radius:8px;padding:10px 6px;text-align:center;font-size:11px;
  font-weight:700;font-family:'Space Mono',monospace;border:1px solid transparent}
.matrix-label{font-size:9px;color:var(--muted);font-family:'Space Mono',monospace;
  display:flex;align-items:center;justify-content:flex-end;padding-right:8px;font-weight:700}

/* TABLE */
.trade-table{width:100%;border-collapse:collapse;font-family:'Space Mono',monospace;font-size:9px}
.trade-table th{color:var(--muted);letter-spacing:1px;padding:8px 10px;text-align:left;
  border-bottom:1px solid var(--border);font-weight:400}
.trade-table td{padding:9px 10px;border-bottom:1px solid #0d180d;vertical-align:middle}
.trade-table tr:last-child td{border-bottom:none}
.trade-table tr:hover td{background:#0d180d50}

/* BADGES */
.badge{border-radius:4px;padding:2px 8px;font-size:9px;font-family:'Space Mono',monospace;
  font-weight:700;white-space:nowrap;display:inline-block}
.badge-win{background:#00C85320;border:1px solid #00C85340;color:var(--green)}
.badge-loss{background:#ff444420;border:1px solid #ff444440;color:var(--red)}
.badge-be{background:#C0C0C020;border:1px solid #C0C0C040;color:var(--silver)}
.badge-trend{background:#00d4ff15;border:1px solid #00d4ff30;color:#00d4ff}
.badge-range{background:#FFD70015;border:1px solid #FFD70030;color:var(--gold)}
.badge-manipulation{background:#e879f915;border:1px solid #e879f930;color:#e879f9}
.badge-easy{background:#00C85315;border:1px solid #00C85330;color:var(--green)}
.badge-medium{background:#FFA50015;border:1px solid #FFA50030;color:var(--orange)}
.badge-hard{background:#ff444415;border:1px solid #ff444430;color:var(--red)}

/* EVOLUTION CHART */
.evo-chart{height:120px;position:relative;margin-top:8px}
.evo-svg{width:100%;height:100%}

/* EMPTY STATE */
.empty{text-align:center;padding:40px;color:var(--muted);font-family:'Space Mono',monospace;font-size:10px}

/* GATE STATS */
.gate-row{display:flex;justify-content:space-between;align-items:center;
  padding:10px 0;border-bottom:1px solid var(--border);font-family:'Space Mono',monospace}
.gate-row:last-child{border-bottom:none}
.gate-key{font-size:10px;color:var(--text)}
.gate-val{font-size:12px;font-weight:700;color:var(--gold)}

@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.card{animation:fadeIn .4s ease both}
.kpi{animation:fadeIn .3s ease both}
</style>
</head>
<body>

<div class="header">
  <div class="logo-area">
    <div class="logo-icon">&#x1F4B0;</div>
    <div>
      <div class="logo-text">TRADING MASTER V5</div>
      <div class="logo-sub">ADMIN DASHBOARD &mdash; ANALYSE SYSTEME</div>
    </div>
  </div>
  <div class="header-right">
    <span class="last-update" id="lastUpdate">--</span>
    <button class="refresh-btn" onclick="loadAll()">&#x21BA; REFRESH</button>
  </div>
</div>

<div class="container">

  <!-- KPI GLOBAUX -->
  <div class="section-title">Performance globale</div>
  <div class="kpi-grid" id="kpiGrid">
    <div class="kpi gold"><div class="kpi-label">TRADES TOTAL</div><div class="kpi-value" id="kTotal">--</div><div class="kpi-sub">journalises</div></div>
    <div class="kpi green"><div class="kpi-label">WINRATE</div><div class="kpi-value" id="kWinrate" style="color:var(--green)">--%</div><div class="kpi-sub" id="kWins">-- wins</div></div>
    <div class="kpi red"><div class="kpi-label">LOSSES</div><div class="kpi-value" id="kLosses" style="color:var(--red)">--</div><div class="kpi-sub" id="kBe">-- BE</div></div>
    <div class="kpi silver"><div class="kpi-label">P&amp;L CUMULE</div><div class="kpi-value" id="kPnl">--</div><div class="kpi-sub">euros</div></div>
    <div class="kpi orange"><div class="kpi-label">TRADES RESTANTS</div><div class="kpi-value" id="kRestants" style="color:var(--orange)">--</div><div class="kpi-sub">objectif 20</div></div>
  </div>

  <!-- WINRATE PAR CONTEXTE + PAR DIFFICULTE -->
  <div class="grid2">
    <div class="card">
      <div class="card-title">Winrate par type de marche</div>
      <div id="ctxBars"><div class="empty">En attente de donnees...</div></div>
    </div>
    <div class="card">
      <div class="card-title">Winrate par difficulte</div>
      <div id="diffBars"><div class="empty">En attente de donnees...</div></div>
    </div>
  </div>

  <!-- MATRICE CONTEXTE x DIFFICULTE -->
  <div class="card" style="margin-bottom:24px">
    <div class="card-title">Matrice Marche x Difficulte (winrate %)</div>
    <div id="matrice"><div class="empty">En attente de donnees...</div></div>
  </div>

  <!-- EVOLUTION P&L + WINRATE GLISSANT -->
  <div class="grid2">
    <div class="card">
      <div class="card-title">Evolution P&amp;L cumule</div>
      <div class="evo-chart"><svg class="evo-svg" id="pnlChart"></svg></div>
    </div>
    <div class="card">
      <div class="card-title">Winrate glissant (10 derniers trades)</div>
      <div class="evo-chart"><svg class="evo-svg" id="wrChart"></svg></div>
    </div>
  </div>

  <!-- STATS PAR PAIRE -->
  <div class="card" style="margin-bottom:24px">
    <div class="card-title">Performance par paire</div>
    <div id="paireBars"><div class="empty">En attente de donnees...</div></div>
  </div>

  <!-- JOURNAL RECENT -->
  <div class="card" style="margin-bottom:24px">
    <div class="card-title">20 derniers trades</div>
    <div style="overflow-x:auto">
      <table class="trade-table">
        <thead>
          <tr>
            <th>#</th><th>DATE</th><th>PAIRE</th><th>TF</th>
            <th>SCORE</th><th>DECISION</th><th>RESULTAT</th>
            <th>MARCHE</th><th>DIFFICULTE</th><th>P&amp;L</th>
          </tr>
        </thead>
        <tbody id="tradeBody"><tr><td colspan="10" class="empty">Chargement...</td></tr></tbody>
      </table>
    </div>
  </div>

  <!-- ============ TABLEAU DE SUIVI DES REGLES ============ -->
  <div class="card" style="margin-top:22px">
    <div class="card-title">SUIVI DES REGLES &mdash; IMPACT SUR LE WINRATE</div>
    <p style="font-size:12px;color:var(--muted);margin:8px 0 14px;font-style:italic">
      Chaque regle activee est mesuree : winrate des trades AVANT sa date vs APRES.
      Rappel : ceci donne des INDICES, pas des preuves (le marche change aussi tout seul).
    </p>
    <div style="overflow-x:auto">
      <table style="width:100%;border-collapse:collapse;font-size:12px">
        <thead>
          <tr style="text-align:left;color:var(--gold);border-bottom:1px solid var(--border)">
            <th style="padding:8px 6px">REGLE</th>
            <th style="padding:8px 6px">DATE</th>
            <th style="padding:8px 6px">WR AVANT</th>
            <th style="padding:8px 6px">WR APRES</th>
            <th style="padding:8px 6px">TRADES AP.</th>
            <th style="padding:8px 6px">IMPACT</th>
          </tr>
        </thead>
        <tbody id="reglesBody">
          <tr><td colspan="6" class="empty">Chargement...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

</div>

<script>
const BACKEND = "https://trading-master-backend.onrender.com";

async function loadAll(){
  document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString('fr-FR');
  try{
    const [statsRes, tradesRes] = await Promise.all([
      fetch(BACKEND+'/journal/stats'),
      fetch(BACKEND+'/journal')
    ]);
    const stats  = await statsRes.json();
    const trades = await tradesRes.json();
    renderKPI(stats, trades);
    renderCtxBars(stats);
    renderDiffBars(stats);
    renderMatrice(trades);
    renderPaireBars(trades);
    renderPnlChart(trades);
    renderWrChart(trades);
    renderTable(trades);
    renderRegles(trades);
  }catch(e){
    console.error('Erreur chargement:', e);
  }
}

function renderKPI(s, trades){
  document.getElementById('kTotal').textContent   = s.total || 0;
  const wr = s.winrate || 0;
  document.getElementById('kWinrate').textContent = wr + '%';
  document.getElementById('kWinrate').style.color = wr >= 50 ? 'var(--green)' : 'var(--red)';
  document.getElementById('kWins').textContent    = (s.wins||0) + ' wins';
  document.getElementById('kLosses').textContent  = s.losses || 0;
  document.getElementById('kBe').textContent      = (s.be||0) + ' BE';
  const pnl = s.total_pnl || 0;
  document.getElementById('kPnl').textContent     = (pnl >= 0 ? '+' : '') + pnl;
  document.getElementById('kPnl').style.color     = pnl >= 0 ? 'var(--green)' : 'var(--red)';
  const restants = Math.max(0, 20 - (s.total||0));
  document.getElementById('kRestants').textContent = restants;
  document.getElementById('kRestants').style.color = restants === 0 ? 'var(--green)' : 'var(--orange)';
}

function renderCtxBars(s){
  const ctxs = [
    {key:'trend',      label:'TREND',        color:'#00d4ff', icon:'📈'},
    {key:'range',      label:'RANGE',        color:'#FFD700', icon:'📦'},
    {key:'manipulation',label:'MANIP',       color:'#e879f9', icon:'🪤'},
  ];
  const el = document.getElementById('ctxBars');
  if(!s.ctx_trend && !s.ctx_range && !s.ctx_manipulation){
    el.innerHTML='<div class="empty">Pas encore de donnees de contexte</div>'; return;
  }
  el.innerHTML = ctxs.map(c => {
    const d = s['ctx_'+c.key] || {total:0,wins:0,winrate:0};
    return `<div class="prog-row">
      <span class="prog-label">${c.icon} ${c.label}</span>
      <div class="prog-bar"><div class="prog-fill" style="width:${d.winrate}%;background:${c.color}"></div></div>
      <span class="prog-val" style="color:${c.color}">${d.winrate}%</span>
      <span class="prog-count">${d.total}T</span>
    </div>`;
  }).join('');
}

function renderDiffBars(s){
  const diffs = [
    {key:'easy',   label:'EASY',   color:'var(--green)',  icon:'🟢'},
    {key:'medium', label:'MEDIUM', color:'var(--orange)', icon:'🟡'},
    {key:'hard',   label:'HARD',   color:'var(--red)',    icon:'🔴'},
  ];
  const el = document.getElementById('diffBars');
  if(!s.diff_easy && !s.diff_medium && !s.diff_hard){
    el.innerHTML='<div class="empty">Pas encore de donnees de difficulte</div>'; return;
  }
  el.innerHTML = diffs.map(d => {
    const data = s['diff_'+d.key] || {total:0,wins:0,winrate:0};
    return `<div class="prog-row">
      <span class="prog-label">${d.icon} ${d.label}</span>
      <div class="prog-bar"><div class="prog-fill" style="width:${data.winrate}%;background:${d.color}"></div></div>
      <span class="prog-val" style="color:${d.color}">${data.winrate}%</span>
      <span class="prog-count">${data.total}T</span>
    </div>`;
  }).join('');
}

function renderMatrice(trades){
  const el = document.getElementById('matrice');
  const done = trades.filter(t => t.resultat && t.contexte_marche && t.difficulte);
  if(done.length === 0){
    el.innerHTML='<div class="empty">Pas encore de donnees combinées</div>'; return;
  }

  const ctxs = ['trend','range','manipulation'];
  const diffs = ['easy','medium','hard'];
  const ctxLabels = {'trend':'📈 TREND','range':'📦 RANGE','manipulation':'🪤 MANIP'};
  const diffLabels = {'easy':'🟢 EASY','medium':'🟡 MEDIUM','hard':'🔴 HARD'};

  // Calculer winrate pour chaque combinaison
  function wr(ctx, diff){
    const filtered = done.filter(t => t.contexte_marche === ctx && t.difficulte === diff);
    if(!filtered.length) return null;
    const wins = filtered.filter(t => t.resultat === 'win').length;
    return {wr: Math.round(wins/filtered.length*100), n: filtered.length};
  }

  function cellColor(pct){
    if(pct === null) return 'background:#0a120a;color:#2a4a2a;border-color:#1a2e1a';
    if(pct >= 70) return `background:#00C85320;color:var(--green);border-color:#00C85340`;
    if(pct >= 50) return `background:#FFD70020;color:var(--gold);border-color:#FFD70040`;
    return `background:#ff444420;color:var(--red);border-color:#ff444440`;
  }

  let html = '<div class="matrix">';
  html += '<div class="matrix-header"></div>';
  diffs.forEach(d => html += `<div class="matrix-header">${diffLabels[d]}</div>`);

  ctxs.forEach(ctx => {
    html += `<div class="matrix-label">${ctxLabels[ctx]}</div>`;
    diffs.forEach(diff => {
      const data = wr(ctx, diff);
      html += `<div class="matrix-cell" style="${cellColor(data ? data.wr : null)}">
        ${data ? data.wr+'%<br><span style="font-size:8px;opacity:.7">${data.n}T</span>' : '--'}
      </div>`;
    });
  });

  html += '</div>';
  el.innerHTML = html;
}

function renderPaireBars(trades){
  const el = document.getElementById('paireBars');
  const done = trades.filter(t => t.resultat && t.pair);
  if(!done.length){ el.innerHTML='<div class="empty">Pas encore de donnees</div>'; return; }

  const paires = {};
  done.forEach(t => {
    if(!paires[t.pair]) paires[t.pair] = {total:0,wins:0};
    paires[t.pair].total++;
    if(t.resultat === 'win') paires[t.pair].wins++;
  });

  const sorted = Object.entries(paires).sort((a,b) => b[1].total - a[1].total);
  el.innerHTML = sorted.map(([pair, d]) => {
    const wr = Math.round(d.wins/d.total*100);
    const col = wr >= 60 ? 'var(--green)' : wr >= 40 ? 'var(--gold)' : 'var(--red)';
    return `<div class="prog-row">
      <span class="prog-label" style="min-width:80px">${pair}</span>
      <div class="prog-bar"><div class="prog-fill" style="width:${wr}%;background:${col}"></div></div>
      <span class="prog-val" style="color:${col}">${wr}%</span>
      <span class="prog-count">${d.total}T</span>
    </div>`;
  }).join('');
}

function renderPnlChart(trades){
  const svg = document.getElementById('pnlChart');
  const withPnl = [...trades].reverse().filter(t => t.pnl != null);
  if(withPnl.length < 2){ svg.innerHTML='<text x="50%" y="50%" fill="#2a4a2a" text-anchor="middle" font-size="10" font-family="monospace">Pas assez de donnees P&L</text>'; return; }

  let cumul = 0;
  const points = withPnl.map(t => { cumul += t.pnl; return cumul; });
  const W = 600, H = 100, pad = 10;
  const minV = Math.min(...points, 0), maxV = Math.max(...points, 1);
  const range = maxV - minV || 1;
  const toX = i => pad + (i/(points.length-1))*(W-2*pad);
  const toY = v => H - pad - ((v-minV)/range)*(H-2*pad);

  const pts = points.map((v,i) => `${toX(i)},${toY(v)}`).join(' ');
  const lastVal = points[points.length-1];
  const lineColor = lastVal >= 0 ? '#00C853' : '#ff4444';

  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.innerHTML = `
    <defs>
      <linearGradient id="pnlGrad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${lineColor}" stop-opacity="0.3"/>
        <stop offset="100%" stop-color="${lineColor}" stop-opacity="0"/>
      </linearGradient>
    </defs>
    <line x1="${pad}" y1="${toY(0)}" x2="${W-pad}" y2="${toY(0)}" stroke="#1a2e1a" stroke-width="1" stroke-dasharray="4,4"/>
    <polyline points="${pts}" fill="none" stroke="${lineColor}" stroke-width="2" stroke-linejoin="round"/>
    <polygon points="${toX(0)},${H-pad} ${pts} ${toX(points.length-1)},${H-pad}" fill="url(#pnlGrad)"/>
    <text x="${W-pad}" y="${toY(lastVal)-4}" fill="${lineColor}" font-size="9" text-anchor="end" font-family="monospace">${lastVal>=0?'+':''}${lastVal.toFixed(0)}E</text>
  `;
}

function renderWrChart(trades){
  const svg = document.getElementById('wrChart');
  const done = [...trades].reverse().filter(t => t.resultat);
  if(done.length < 3){ svg.innerHTML='<text x="50%" y="50%" fill="#2a4a2a" text-anchor="middle" font-size="10" font-family="monospace">Minimum 3 trades</text>'; return; }

  const window = 10;
  const wrs = [];
  for(let i = 0; i < done.length; i++){
    const slice = done.slice(Math.max(0, i-window+1), i+1);
    const wins = slice.filter(t => t.resultat === 'win').length;
    wrs.push(Math.round(wins/slice.length*100));
  }

  const W = 600, H = 100, pad = 10;
  const toX = i => pad + (i/(wrs.length-1))*(W-2*pad);
  const toY = v => H - pad - (v/100)*(H-2*pad);
  const pts = wrs.map((v,i) => `${toX(i)},${toY(v)}`).join(' ');
  const last = wrs[wrs.length-1];
  const col = last >= 50 ? '#00C853' : '#ff4444';

  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.innerHTML = `
    <line x1="${pad}" y1="${toY(50)}" x2="${W-pad}" y2="${toY(50)}" stroke="#1a2e1a" stroke-width="1" stroke-dasharray="4,4"/>
    <text x="${pad+2}" y="${toY(50)-3}" fill="#2a4a2a" font-size="8" font-family="monospace">50%</text>
    <polyline points="${pts}" fill="none" stroke="${col}" stroke-width="2" stroke-linejoin="round"/>
    <circle cx="${toX(wrs.length-1)}" cy="${toY(last)}" r="4" fill="${col}"/>
    <text x="${toX(wrs.length-1)+6}" y="${toY(last)+4}" fill="${col}" font-size="9" font-family="monospace">${last}%</text>
  `;
}

function renderTable(trades){
  const tbody = document.getElementById('tradeBody');
  const recent = trades.slice(0, 20);
  if(!recent.length){ tbody.innerHTML='<tr><td colspan="10" class="empty">Aucun trade</td></tr>'; return; }

  const DC = {ATTENDRE:'#ff6666',SIGNAL:'#FFD700',EXECUTION:'#00C853',MANUEL:'#C0C0C0'};

  tbody.innerHTML = recent.map(t => {
    const rc = t.resultat ? `<span class="badge badge-${t.resultat}">${t.resultat?.toUpperCase()}</span>` : '<span style="color:#2a4a2a;font-size:9px">--</span>';
    const cc = t.contexte_marche ? `<span class="badge badge-${t.contexte_marche}">${t.contexte_marche?.toUpperCase()}</span>` : '<span style="color:#2a4a2a">--</span>';
    const dc = t.difficulte ? `<span class="badge badge-${t.difficulte}">${t.difficulte?.toUpperCase()}</span>` : '<span style="color:#2a4a2a">--</span>';
    const pnl = t.pnl != null ? `<span style="color:${t.pnl>=0?'var(--green)':'var(--red)'}; font-weight:700">${t.pnl>=0?'+':''}${t.pnl}E</span>` : '--';
    const dec = `<span style="color:${DC[t.decision]||'#888'};font-size:9px">${t.decision||'--'}</span>`;
    return `<tr>
      <td style="color:var(--muted)">${t.id}</td>
      <td style="color:var(--muted)">${(t.date||'').substring(0,16)}</td>
      <td style="color:var(--gold);font-weight:700">${t.pair||'--'}</td>
      <td style="color:var(--silver)">${t.tf||'--'}</td>
      <td style="color:var(--text)">${t.score||'--'}</td>
      <td>${dec}</td>
      <td>${rc}</td>
      <td>${cc}</td>
      <td>${dc}</td>
      <td>${pnl}</td>
    </tr>`;
  }).join('');
}

// Chargement initial + refresh auto toutes les 60s
// ============ SUIVI DES REGLES ============
// LISTE DES REGLES : ajoute une ligne ici a chaque nouvelle regle activee.
// date au format 'AAAA-MM-JJ'. Le calcul avant/apres se fait automatiquement.
const REGLES = [
  {nom:"POINT ZERO (etat systeme)", date:"2026-07-19", note:"reference"},
  {nom:"Garde-fou RSI (fail-closed)", date:"2026-07-18", note:"bloque entrees si RSI inconnu ou extreme"},
  {nom:"Daily crypto via Binance", date:"2026-07-18", note:"vrai Daily pour BTC/ETH/SOL"},
  {nom:"Penalite Daily manquant (-15)", date:"2026-07-18", note:"score reduit si pas de Daily"},
  {nom:"Detecteur de regime (observation)", date:"2026-07-19", note:"TREND/RANGE/INDECIS, sans pouvoir"},
  // Pour ajouter une regle : copie une ligne ci-dessus et change nom + date.
];

// Calcule le winrate (win / (win+loss+be)) sur une liste de trades
function winrateDe(trades){
  let w=0, l=0, be=0;
  trades.forEach(t=>{
    if(t.resultat==='win') w++;
    else if(t.resultat==='loss') l++;
    else if(t.resultat==='be') be++;
  });
  const pris = w+l+be;
  return pris>0 ? {wr:Math.round(w/pris*100), n:pris} : {wr:null, n:0};
}

// Convertit la date d'un trade (created_at ISO, ou date FR) en objet Date comparable
function dateTrade(t){
  const s = t.created_at || t.date || '';
  // created_at est en ISO (2026-07-18T...), directement parsable
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}

function renderRegles(trades){
  const body = document.getElementById('reglesBody');
  if(!body) return;
  if(!trades || !trades.length){
    body.innerHTML = '<tr><td colspan="6" class="empty">Aucun trade</td></tr>';
    return;
  }
  // Trie les regles par date croissante
  const regles = [...REGLES].sort((a,b)=> new Date(a.date) - new Date(b.date));
  body.innerHTML = regles.map(r=>{
    const dRegle = new Date(r.date);
    const avant = trades.filter(t=>{ const d=dateTrade(t); return d && d < dRegle; });
    const apres = trades.filter(t=>{ const d=dateTrade(t); return d && d >= dRegle; });
    const wrAv = winrateDe(avant);
    const wrAp = winrateDe(apres);
    // Impact = difference de winrate, seulement si assez de donnees des deux cotes
    let impact = '--', impactColor = 'var(--muted)';
    if(wrAv.wr!==null && wrAp.wr!==null && wrAv.n>=5 && wrAp.n>=5){
      const diff = wrAp.wr - wrAv.wr;
      impact = (diff>=0?'+':'') + diff + ' pts';
      impactColor = diff>0 ? 'var(--green)' : diff<0 ? 'var(--red)' : 'var(--muted)';
    } else if(wrAp.n < 5){
      impact = 'N<5 (attendre)';
    } else {
      impact = 'pas de "avant"';
    }
    const avTxt = wrAv.wr!==null ? (wrAv.wr+'% ('+wrAv.n+'T)') : '--';
    const apTxt = wrAp.wr!==null ? (wrAp.wr+'% ('+wrAp.n+'T)') : '--';
    return '<tr style="border-bottom:1px solid var(--border)">'
      + '<td style="padding:8px 6px;color:var(--text)">'+r.nom+'</td>'
      + '<td style="padding:8px 6px;color:var(--muted);font-family:monospace">'+r.date+'</td>'
      + '<td style="padding:8px 6px;color:var(--silver)">'+avTxt+'</td>'
      + '<td style="padding:8px 6px;color:var(--silver)">'+apTxt+'</td>'
      + '<td style="padding:8px 6px;color:var(--muted)">'+wrAp.n+'</td>'
      + '<td style="padding:8px 6px;font-weight:700;color:'+impactColor+'">'+impact+'</td>'
      + '</tr>';
  }).join('');
}

loadAll();
setInterval(loadAll, 60000);
</script>
</body>
</html>
