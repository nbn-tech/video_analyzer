const form = document.getElementById('upload-form');
const fileInput = document.getElementById('video-file');
const statusEl = document.getElementById('status');

const metaSection = document.getElementById('meta');
const metaContent = document.getElementById('meta-content');

const cornersSection = document.getElementById('corners');
const cornersBody = document.querySelector('#result-table tbody');

const audioSection = document.getElementById('audio');
const audioBody = document.querySelector('#audio-table tbody');

const ocrSection = document.getElementById('ocr');
const ocrHeading = document.querySelector('#ocr h2');
const ocrBody = document.querySelector('#ocr-table tbody');

function resetTables() {
  cornersBody.innerHTML = '';
  audioBody.innerHTML = '';
  ocrBody.innerHTML = '';
  metaContent.innerHTML = '';

  metaSection.hidden = true;
  cornersSection.hidden = true;
  audioSection.hidden = true;
  ocrSection.hidden = true;
}

function formatTime(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return m > 0 ? `${m}分${s}秒` : `${s}秒`;
}

function appendRow(tbody, cells) {
  const tr = document.createElement('tr');
  tr.innerHTML = cells.map((cell) => `<td>${cell}</td>`).join('');
  tbody.appendChild(tr);
}

async function runAnalysis(mode) {
  if (!fileInput.files.length) return;

  const data = new FormData();
  data.append('file', fileInput.files[0]);

  const modeLabel = mode === 'vision' ? 'Vision解析' : 'OCR解析';
  statusEl.textContent = `${modeLabel}中です。動画の長さによって数分かかります。`;
  resetTables();

  const endpoint = mode === 'vision' ? '/api/upload/vision' : '/api/upload';

  try {
    const res = await fetch(endpoint, { method: 'POST', body: data });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    const payload = await res.json();
    const isVision = payload.mode === 'vision';
    const badge = `<span class="mode-badge ${isVision ? 'vision' : 'ocr'}">${isVision ? 'Vision' : 'OCR'}</span>`;

    const processedName = payload.processed_filename ?? '(生成なし)';
    const extraInfo = isVision
      ? `<p><strong>キーフレーム数:</strong> ${payload.keyframe_count}</p>`
      : `<p><strong>OCR行数:</strong> ${payload.ocr_rows.length}</p>`;

    metaContent.innerHTML = `
      <p><strong>解析モード:</strong> ${badge}</p>
      <p><strong>元動画:</strong> ${payload.filename}</p>
      <p><strong>白黒化動画:</strong> ${processedName}</p>
      <p><strong>Whisper行数:</strong> ${payload.audio_rows.length}</p>
      ${extraInfo}
      <p><strong>コーナー数:</strong> ${payload.corners.length}</p>
    `;
    metaSection.hidden = false;

    if (!payload.corners.length) {
      appendRow(cornersBody, ['-', '-', '分類なし', 'Geminiの返却が空でした。入力テキストを確認してください。']);
    } else {
      payload.corners.forEach((c) => {
        const tags = (c.tags || []).map(t => `<span class="tag">${t}</span>`).join('');
        appendRow(cornersBody, [
          formatTime(c.start_sec),
          formatTime(c.end_sec),
          c.title,
          c.summary,
          tags,
        ]);
      });
    }
    cornersSection.hidden = false;

    payload.audio_rows.forEach((r) => {
      appendRow(audioBody, [r.start_sec.toFixed(1), r.end_sec.toFixed(1), r.text]);
    });
    audioSection.hidden = payload.audio_rows.length === 0;

    if (!isVision) {
      ocrHeading.textContent = 'PaddleOCR 抽出テキスト';
      payload.ocr_rows.forEach((r) => {
        appendRow(ocrBody, [r.start_sec.toFixed(1), r.end_sec.toFixed(1), r.text]);
      });
      ocrSection.hidden = payload.ocr_rows.length === 0;
    } else {
      ocrSection.hidden = true;
    }

    statusEl.textContent = `${modeLabel}完了: ${payload.filename}`;
  } catch (err) {
    statusEl.textContent = `エラー: ${err.message}`;
  }
}

form.addEventListener('submit', (e) => {
  e.preventDefault();
  const btn = e.submitter;
  const mode = btn?.dataset.mode ?? 'ocr';
  runAnalysis(mode);
});
