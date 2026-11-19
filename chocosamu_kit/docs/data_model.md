# データの形

## 回ごと: `rows/<回>/rows.json`（`samunet_summarize.py` の出力）

```jsonc
{
  "episode_no": 4,
  "episode": {
    "broadcast_date": "2024-01-14",          // スレートからプログラムで決めた値
    "broadcast_date_gemini": "2024-01-14",   // 参考：Gemini が出した値
    "date_note": "",                         // 年を推定した場合などの注記
    "pref": "愛知県", "city": "常滑市", "area": "",
    "location": "愛知県常滑市",              // 表示用（pref+city+（area））
    "talents": [{"name": "椿鬼奴", "group": ""}, {"name": "村上健志", "group": "フルーツポンチ"}],
    "mc": ["チョコレートプラネット"]
  },
  "rows": [
    {
      "start_sec": 59, "end_sec": 248,       // ロケ VTR の区間（完パケの秒）
      "names": ["紗来"], "ages": [17],       // 夫婦・家族は1行に全員
      "name_note": "ふりがな: さき",
      "title": "クレしんモノマネの女性 実はチアダンス世界…",  // 右上の小見出し
      "place": "常滑駅前",                   // 取材場所（店名のことも）
      "talents": ["椿鬼奴", "村上健志"],     // episode.talents の name
      "summary": "…",
      "genre": "スーパーキッズ",             // 並び順・フード併記はプログラムでそろえた値
      "genre_raw": "スーパーキッズ",         // 参考：Gemini の生の値
      "genre_reason": "…",
      "thumbnail_index": 1,                  // thumbs/<回>/thumbnails.json の index（無ければ null）
      "thumbnail_copy": "若きチアダンサー 全米を魅了",
      "is_top": false
    }
  ]
}
```

同じフォルダに、`rows.csv`（1超町人＝1行）、`prompt.txt`（Gemini に渡したテキスト）、`name_frames/`（名前テロップの画像）ができる。

## サイト用: `site_data/data.json`（`build_site_data.py` の出力）

```jsonc
{
  "episodes": [
    {"episode": 4, "broadcast_date": "2024-01-14", "date_note": "", "pref": "愛知県", "city": "常滑市", "area": "",
     "talents": [{"name": "椿鬼奴", "group": ""}, …], "mc": ["チョコレートプラネット"]}
  ],
  "people": [
    {"id": "004-01", "episode": 4, "order": 1,
     "names": ["紗来"], "ages": [17], "name_note": "ふりがな: さき",
     "title": "…", "place": "常滑駅前", "talents": ["椿鬼奴", "村上健志"],
     "summary": "…", "genres": ["スーパーキッズ"], "genre_reason": "…",   // 該当なしは空配列
     "start_sec": 59, "end_sec": 248,
     "thumbnail": "thumbs/004/thumb_01.jpg", "thumbnail_shown_sec": 169.5, "thumbnail_copy": "…",
     "is_top": false}
  ]
}
```

- `id` は「回3桁-その回での順番2桁」。作り直すと順番が変わることがあるので、人が修正したデータを持つなら、別に安定した ID を振る仕組みが要る。
- 全回で約1,100行・1MB程度の見込み。

## 将来足したい項目

- `readings`：名前のよみ（今は `name_note` の文章に入っている）
- 視聴率：その超町人の放送区間（`start_sec`〜`end_sec` を放送時刻に変換）の毎分視聴率
- 人が確認・修正したかどうかのフラグと、修正履歴
