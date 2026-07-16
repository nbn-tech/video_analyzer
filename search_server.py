"""
動画分析結果検索サーバー

起動方法:
    .venv/Scripts/python search_server.py

http://localhost:8000 でブラウザから検索できる。
"""

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse
from app.athena import run_athena_query
from app.config import settings

app = FastAPI(title="番組検索")


@app.get("/search")
def search(
    q: str = Query(..., description="検索キーワード"),
    channel: str = Query("ch6", pattern=r"^ch[0-9]+$"),
    day_of_week: str | None = Query(None, pattern=r"^(mon|tue|wed|thu|fri|sat|sun)$"),
):
    """分析結果からキーワード検索し、動画名・開始・終了時刻を返す。"""
    safe_q = q.replace("'", "''")
    table = f'"{settings.athena_glue_db}"."{settings.athena_glue_table}"'
    weekday_filter = f"AND day_of_week = '{day_of_week}'" if day_of_week else ""
    sql = f"""
        SELECT
            broadcast_date,
            channel,
            filename,
            start_sec,
            end_sec,
            title,
            summary,
            tags
        FROM {table}
        WHERE channel = '{channel}'
          {weekday_filter}
          AND (
              title LIKE '%{safe_q}%'
              OR summary LIKE '%{safe_q}%'
              OR tags LIKE '%{safe_q}%'
          )
        ORDER BY broadcast_date DESC, program_start_sec, start_sec
    """
    try:
        rows = run_athena_query(sql)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "keyword": q,
        "channel": channel,
        "day_of_week": day_of_week,
        "count": len(rows),
        "results": rows,
    }


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
<h1>番組分析結果検索</h1>
<div class="search-box">
  <select id="channel">
    <option value="ch1">ch1</option>
    <option value="ch6" selected>ch6</option>
  </select>
  <select id="day-of-week">
    <option value="">全曜日</option>
    <option value="mon">月曜日</option>
    <option value="tue">火曜日</option>
    <option value="wed">水曜日</option>
    <option value="thu">木曜日</option>
    <option value="fri">金曜日</option>
    <option value="sat">土曜日</option>
    <option value="sun">日曜日</option>
  </select>
  <input type="text" id="q" placeholder="キーワードを入力（例：大谷）" onkeydown="if(event.key==='Enter')doSearch()">
  <button id="btn" onclick="doSearch()">検索</button>
</div>
<div id="status"></div>
<div id="result"></div>
<script>
async function doSearch() {
  const q = document.getElementById('q').value.trim();
  const channel = document.getElementById('channel').value;
  const dayOfWeek = document.getElementById('day-of-week').value;
  if (!q) return;
  const btn = document.getElementById('btn');
  const status = document.getElementById('status');
  const result = document.getElementById('result');
  btn.disabled = true;
  status.textContent = 'Athenaクエリ実行中...';
  result.innerHTML = '';
  try {
    const params = new URLSearchParams({ q, channel });
    if (dayOfWeek) params.set('day_of_week', dayOfWeek);
    const r = await fetch('/search?' + params.toString());
    const data = await r.json();
    if (!r.ok) { status.textContent = 'エラー: ' + (data.detail || r.status); return; }
    status.textContent = `${data.count}件見つかりました`;
    if (data.count === 0) { result.innerHTML = '<p>該当なし</p>'; return; }
    let html = '<table><tr><th>動画ファイル</th><th>開始</th><th>終了</th><th>タイトル</th><th>要約</th></tr>';
    for (const row of data.results) {
      const fname = row.filename;
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
