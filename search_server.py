"""
アノテーション検索サーバー

起動方法:
    .venv/Scripts/python search_server.py

http://localhost:8000 でブラウザから検索できる。
"""

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse
from app.athena import run_athena_query

app = FastAPI(title="番組検索")


@app.get("/search")
def search(q: str = Query(..., description="検索キーワード")):
    """アノテーションからキーワード検索し、動画名・開始・終了時刻を返す"""
    safe_q = q.replace("'", "''")
    sql = f"""
        SELECT
            object_key,
            name,
            split_part(text_value, ',', 1) AS start_sec,
            split_part(text_value, ',', 2) AS end_sec,
            split_part(text_value, ',', 3) AS title,
            split_part(text_value, ',', 4) AS summary
        FROM {GLUE_TABLE}
        WHERE name LIKE 'corner\_%' ESCAPE '\\'
          AND (
            split_part(text_value, ',', 3) LIKE '%{safe_q}%'
            OR split_part(text_value, ',', 4) LIKE '%{safe_q}%'
            OR split_part(text_value, ',', 5) LIKE '%{safe_q}%'
          )
        ORDER BY object_key, CAST(split_part(text_value, ',', 1) AS DOUBLE)
    """
    try:
        rows = run_athena_query(sql)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"keyword": q, "count": len(rows), "results": rows}


@app.get("/", response_class=HTMLResponse)
def index():
    return """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>番組検索</title>
<style>
  body { font-family: sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; background: #f5f5f5; }
  h1 { font-size: 1.4em; color: #333; }
  .search-box { display: flex; gap: 8px; margin: 20px 0; }
  input { flex: 1; padding: 10px; font-size: 1em; border: 1px solid #ccc; border-radius: 4px; }
  button { padding: 10px 24px; background: #0066cc; color: #fff; border: none; border-radius: 4px; cursor: pointer; font-size: 1em; }
  button:disabled { background: #aaa; }
  #status { color: #666; margin: 8px 0; min-height: 1.4em; }
  table { width: 100%; border-collapse: collapse; background: #fff; margin-top: 12px; }
  th { background: #444; color: #fff; padding: 8px 12px; text-align: left; font-size: 0.85em; }
  td { padding: 7px 12px; border-bottom: 1px solid #eee; font-size: 0.85em; vertical-align: top; }
  tr:hover td { background: #f0f4ff; }
  .filename { color: #666; font-size: 0.8em; }
</style>
</head>
<body>
<h1>番組アノテーション検索</h1>
<div class="search-box">
  <input type="text" id="q" placeholder="キーワードを入力（例：大谷）" onkeydown="if(event.key==='Enter')doSearch()">
  <button id="btn" onclick="doSearch()">検索</button>
</div>
<div id="status"></div>
<div id="result"></div>
<script>
async function doSearch() {
  const q = document.getElementById('q').value.trim();
  if (!q) return;
  const btn = document.getElementById('btn');
  const status = document.getElementById('status');
  const result = document.getElementById('result');
  btn.disabled = true;
  status.textContent = 'Athenaクエリ実行中...';
  result.innerHTML = '';
  try {
    const r = await fetch('/search?q=' + encodeURIComponent(q));
    const data = await r.json();
    if (!r.ok) { status.textContent = 'エラー: ' + (data.detail || r.status); return; }
    status.textContent = `${data.count}件見つかりました`;
    if (data.count === 0) { result.innerHTML = '<p>該当なし</p>'; return; }
    let html = '<table><tr><th>動画ファイル</th><th>開始</th><th>終了</th><th>タイトル</th><th>要約</th></tr>';
    for (const row of data.results) {
      const fname = row.object_key.split('/').pop();
      html += `<tr>
        <td class="filename">${fname}</td>
        <td>${row.start_sec}</td>
        <td>${row.end_sec}</td>
        <td>${row.title}</td>
        <td>${row.summary}</td>
      </tr>`;
    }
    html += '</table>';
    result.innerHTML = html;
  } catch(e) {
    status.textContent = 'エラー: ' + e.message;
  } finally {
    btn.disabled = false;
  }
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
