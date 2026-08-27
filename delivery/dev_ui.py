"""本機測試介面（dev 模式限定）：瀏覽器模擬 LINE 對話。

走跟正式 webhook 完全相同的 _handle_text 邏輯，只是訊息改進 DEV_OUTBOX。
支援 Flex Message 簡易渲染（文字 + 按鈕），背景生成的 Push 通知靠輪詢送達。
"""

DEV_HTML = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>YT 課程策展 — 測試介面</title>
<style>
  :root { --line-green:#06c755; --bg:#8cabd9; }
  * { box-sizing:border-box; margin:0; }
  body { font-family:-apple-system,"PingFang TC","Noto Sans TC",sans-serif;
         background:#f0f2f5; display:flex; justify-content:center; min-height:100vh; }
  #app { width:100%; max-width:480px; display:flex; flex-direction:column; height:100vh; }
  header { background:var(--line-green); color:#fff; padding:12px 16px; font-weight:600;
           display:flex; justify-content:space-between; align-items:center; }
  header small { font-weight:400; opacity:.85; }
  #chat { flex:1; overflow-y:auto; background:var(--bg); padding:12px; }
  .msg { max-width:82%; margin:6px 0; padding:10px 12px; border-radius:16px;
         white-space:pre-wrap; word-break:break-word; font-size:15px; line-height:1.5; }
  .bot { background:#fff; border-top-left-radius:4px; }
  .me  { background:#9ef01a; margin-left:auto; border-top-right-radius:4px; }
  .flex-card { background:#fff; border-radius:12px; padding:12px; margin:6px 0; max-width:88%;
               box-shadow:0 1px 3px rgba(0,0,0,.15); }
  .flex-card .t-bold { font-weight:700; font-size:16px; margin:2px 0; }
  .flex-card .t-small { font-size:12px; color:#888; margin:2px 0; }
  .flex-card .t { font-size:14px; margin:4px 0; }
  .flex-card hr { border:none; border-top:1px solid #eee; margin:8px 0; }
  .flex-card a.btn, .flex-card button.btn { display:block; width:100%; text-align:center;
      padding:10px; margin-top:8px; border-radius:8px; border:none; cursor:pointer;
      font-size:14px; text-decoration:none; }
  .btn.primary { background:var(--line-green); color:#fff; }
  .btn.secondary { background:#eee; color:#333; }
  #quick { display:flex; gap:6px; overflow-x:auto; padding:8px; background:#fff; border-top:1px solid #ddd; }
  #quick button { flex:none; padding:6px 12px; border-radius:14px; border:1px solid var(--line-green);
                  background:#fff; color:var(--line-green); font-size:13px; cursor:pointer; }
  #inputbar { display:flex; gap:8px; padding:10px; background:#fff; }
  #inputbar input { flex:1; padding:10px 14px; border:1px solid #ccc; border-radius:20px; font-size:15px; }
  #inputbar button { padding:10px 18px; border:none; border-radius:20px;
                     background:var(--line-green); color:#fff; font-size:15px; cursor:pointer; }
</style>
</head>
<body>
<div id="app">
  <header><span>🎓 YT 課程策展（測試）</span><small>dev 模式・不會發到 LINE</small></header>
  <div id="chat"></div>
  <div id="quick">
    <button onclick="send('開課')">📚 開課（逐步引導）</button>
    <button onclick="send('今日課程')">📖 今日課程</button>
    <button onclick="send('完成')">✅ 完成</button>
    <button onclick="send('課程列表')">📋 課程列表</button>
    <button onclick="send('複習')">🧠 複習</button>
    <button onclick="window.open('/dev/export','_blank')">📥 匯出課綱</button>
    <button onclick="send('設定')">⚙️ 設定</button>
    <button onclick="send('我的課程')">我的課程</button>
    <button onclick="send('我的點數')">我的點數</button>
  </div>
  <div id="inputbar">
    <input id="input" placeholder="輸入訊息…" autocomplete="off">
    <button onclick="sendInput()">送出</button>
  </div>
</div>
<script>
const chat = document.getElementById('chat');
const input = document.getElementById('input');
// isComposing / keyCode 229：注音等輸入法選字中的 Enter 不觸發送出
input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) sendInput();
});

function el(tag, cls, text) {
  const d = document.createElement(tag);
  if (cls) d.className = cls;
  if (text !== undefined) d.textContent = text;
  return d;
}
function scroll() { chat.scrollTop = chat.scrollHeight; }

function renderBot(msg) {
  if (msg.type === 'text') { chat.appendChild(el('div','msg bot', msg.text)); scroll(); return; }
  if (msg.type === 'flex') { renderFlex(msg.contents, msg.altText); scroll(); return; }
  chat.appendChild(el('div','msg bot', JSON.stringify(msg))); scroll();
}

function renderFlex(bubble, alt) {
  const card = el('div','flex-card');
  walk(bubble, card);
  if (!card.childNodes.length) card.appendChild(el('div','t', alt || '(flex message)'));
  chat.appendChild(card);
}
function walk(node, card) {
  if (!node || typeof node !== 'object') return;
  if (Array.isArray(node)) { node.forEach(n => walk(n, card)); return; }
  if (node.type === 'text') {
    const cls = node.weight === 'bold' ? 't-bold' : (node.size === 'xs' || node.size === 'sm' ? 't-small' : 't');
    card.appendChild(el('div', cls, node.text));
  } else if (node.type === 'separator') {
    card.appendChild(el('hr'));
  } else if (node.type === 'button' && node.action) {
    if (node.action.type === 'uri') {
      const a = el('a', 'btn ' + (node.style || 'primary'), node.action.label);
      a.href = node.action.uri; a.target = '_blank';
      card.appendChild(a);
    } else if (node.action.type === 'message') {
      const b = el('button', 'btn ' + (node.style || 'secondary'), node.action.label);
      b.onclick = () => send(node.action.text);
      card.appendChild(b);
    }
  } else {
    ['header','hero','body','footer','contents'].forEach(k => { if (node[k]) walk(node[k], card); });
  }
}

async function send(text) {
  chat.appendChild(el('div','msg me', text)); scroll();
  const typing = el('div','msg bot','…處理中');  // 探測/生成可能要幾秒，先給回饋
  chat.appendChild(typing); scroll();
  try {
    const r = await fetch('/dev/message', { method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({text}) });
    const data = await r.json();
    typing.remove();
    (data.messages || []).forEach(renderBot);
  } catch (e) {
    typing.remove();
    chat.appendChild(el('div','msg bot','⚠️ 連線失敗：' + e)); scroll();
  }
}
function sendInput() { const t = input.value.trim(); if (t) { input.value=''; send(t); } }

// 輪詢背景 Push（例如課程生成完成通知）
setInterval(async () => {
  try {
    const r = await fetch('/dev/outbox');
    const data = await r.json();
    (data.messages || []).forEach(renderBot);
  } catch (e) {}
}, 2000);

renderBot({type:'text', text:'👋 這是本機測試介面，行為與 LINE 版完全相同。\\n\\n【測法 A：自備 API key（免填 .env）】\\n1. 點「設定」→ 2 自備 API key → 選品牌 → 貼 key（此步驟開發者可改回「訂閱」走訂閱制）→ 選模型\\n2. 再「設定」→ 3 貼 YouTube API key\\n3. 點「開課 AI工作流 5」→ 確認（不扣點）\\n\\n【測法 B：點數制（需在 .env 填平台 key）】\\n1. 點「儲值 100」（測試專用）\\n2. 開課 → 確認扣點 → 等完成通知\\n\\n之後都一樣：「今日課程」→「完成」→「答案」'});
</script>
</body>
</html>"""
