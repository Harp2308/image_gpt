const API_BASE = "";
let citationMap = {};
let sidebarOpen = true;
let darkMode = false;

function toggleSidebar() {
  sidebarOpen = !sidebarOpen;
  document.getElementById("sidebar").classList.toggle("collapsed", !sidebarOpen);
  document.getElementById("sidebarToggleBtn").textContent = sidebarOpen ? "☰" : "›";
}

function toggleTheme() {
  darkMode = !darkMode;
  document.documentElement.setAttribute("data-theme", darkMode ? "dark" : "light");
  document.getElementById("themeIcon").textContent = darkMode ? "☀️" : "🌙";
  document.getElementById("themeLabel").textContent = darkMode ? "Light" : "Dark";
}

function ea(s) {
  return (s||"").replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

function setStatus(cls, msg) {
  const el = document.getElementById("status");
  el.className = "status-line" + (cls ? " "+cls : "");
  el.textContent = msg;
}

async function runQuery() {
  const query = document.getElementById("queryInput").value.trim();
  if (!query) return;
  const topK = parseInt(document.getElementById("topK").value) || 5;
  document.getElementById("sendBtn").disabled = true;
  setStatus("", "Fetching…");
  document.getElementById("answerWrap").innerHTML = '<div class="empty-state">Loading…</div>';
  document.getElementById("docList").innerHTML = '';
  try {
    const resp = await fetch(`${API_BASE}/query`, {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({query, top_k:topK})
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    citationMap = {};
    (data.citations||[]).forEach(c => { citationMap[c.marker] = c; });
    renderAnswer(data.answer, data.citations||[]);
    renderDocList(data.citations||[]);
    setStatus("ok", `${(data.citations||[]).length} citations | ${data.language||"?"}`);
  } catch(e) {
    setStatus("error", `Error: ${e.message}`);
    document.getElementById("answerWrap").innerHTML = '<div class="empty-state">Request failed.</div>';
  } finally {
    document.getElementById("sendBtn").disabled = false;
  }
}

function renderAnswer(text, citations) {
  const wrap = document.getElementById("answerWrap");
  wrap.innerHTML = "";

  const titleMatch = text.match(/^#\s+(.+)$/m);
  const title = titleMatch ? titleMatch[1] : "Answer";

  const titleRow = document.createElement("div");
  titleRow.className = "answer-title-row";
  titleRow.innerHTML = `<div class="answer-title">${ea(title)}</div><span class="lang-chip">${ea(detectLang(text))}</span>`;
  wrap.appendChild(titleRow);

  const body = text.replace(/^#\s+.+$/m,"").trim();
  const lines = body.split("\n");

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line || line === "---") continue;

    if (line.startsWith("## ")) {
      const sh = document.createElement("div");
      sh.className = "section-head";
      sh.textContent = line.slice(3);
      wrap.appendChild(sh);
      continue;
    }

    const stepMatch = line.match(/^(\d+)\.\s+([\s\S]+)$/);
    if (stepMatch) {
      wrap.appendChild(buildStepCard(stepMatch[1], stepMatch[2]));
      continue;
    }

    const p = document.createElement("p");
    p.className = "prose-p";
    p.innerHTML = fmtInline(line);
    wrap.appendChild(p);
  }
}

function buildStepCard(num, rawText) {
  const markerRe = /🖼️\[page\s+(\d+)\s*\|\s*entry\s+([\w-]+)\]/g;
  const refs = [];
  let m;
  while ((m = markerRe.exec(rawText)) !== null) {
    refs.push({ key: `[page ${m[1]} | entry ${m[2]}]`, pg: m[1], eid: m[2] });
  }

  const cleanText = rawText.replace(/🖼️\[page\s+(\d+)\s*\|\s*entry\s+([\w-]+)\]/g, "");

  const card = document.createElement("div");
  card.className = "step-card";

  let imgHtml = "";
  for (const ref of refs) {
    const c = citationMap[ref.key];
    if (c && c.image_url) {
      const url = ea(`${API_BASE}${c.image_url}`);
      imgHtml += `<div class="step-img-block" onclick="openModal('${ea(ref.key)}')"><img src="${url}" alt="${ea(ref.key)}" loading="lazy"></div>`;
    }
  }

  card.innerHTML = `
    <span class="step-num">${num}</span>
    <div class="step-body">
      <div class="step-text">${fmtInline(cleanText)}</div>
      ${imgHtml}
    </div>`;
  return card;
}

function renderDocList(citations) {
  const list = document.getElementById("docList");
  const seen = new Set();
  citations.forEach(c => { if (c.source_file) seen.add(c.source_file); });
  if (!seen.size) { list.innerHTML = ''; return; }
  list.innerHTML = [...seen].map(name => `
    <div class="doc-row">
      <div class="doc-icon">📄</div>
      <span class="doc-name">${ea(name)}</span>
    </div>`).join('');
}

function fmtInline(s) {
  return s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
}

function detectLang(text) {
  return /\b(para|como|processo|linha|passo|deve|ajustar|limpar|retirar)\b/i.test(text) ? "PT" : "EN";
}

function openModal(key) {
  const c = citationMap[key];
  if (!c || !c.image_url) return;
  document.getElementById("modalImg").src = `${API_BASE}${c.image_url}`;
  document.getElementById("modalMarker").textContent = c.marker || key;
  document.getElementById("modalDesc").textContent = c.image_description || "";
  document.getElementById("modalOverlay").classList.add("open");
}

function closeModal() {
  document.getElementById("modalOverlay").classList.remove("open");
}

function handleModalClick(e) {
  if (e.target === document.getElementById("modalOverlay")) closeModal();
}

document.getElementById("queryInput").addEventListener("keydown", e => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) runQuery();
});
