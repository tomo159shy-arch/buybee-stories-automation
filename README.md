# BuyBee Stories 自動化

家具の動画（複数本まとめてOK）をアップロードすると、シーン（カット）ごとにGeminiが
キャッチコピー・特徴・価格・ハッシュタグ・ワンポイントスタンプを生成し、
Stories用の縦動画（9:16）に自然なクロスフェードで焼き込んで書き出すStreamlitアプリ。

## セットアップ

```bash
cd buybee_stories_automation
pip install -r requirements.txt
streamlit run app.py
```

ブラウザで http://localhost:8501 を開く。ffmpegがPATHに必要（`ffmpeg -version` で確認）。

## 使い方

1. **① 動画をアップロード** — mp4/mov/avi、複数選択可。
2. **② テロップの雰囲気を選んで分析** — スタイル（煽り系/かわいい系/情報系）を選択。
   商品ページのURLを貼ると、そのページの情報も加味して生成する（任意）。
   「分析する」を押すとシーンを自動検出し、シーンごとにGeminiが内容を生成する。
3. **③ 動画ごとに内容を確認・編集** — シーンごとにタブで、キャッチコピー・特徴・価格・
   ハッシュタグ・文字の大きさ・フォント・文字の色・スタンプ文言・スタンプ色を編集できる。
   「🔄 再生成」でスタンプ文言・色だけをAPIを使わず即座にシャッフルできる。
   「このシーンには入れない」チェックでそのシーンだけテロップ・スタンプを外せる。
4. **④ 動画を生成** — まとめて書き出し、1本ごとに完成したらWindows通知が届く。
   完成した動画はその場でプレビュー・ダウンロード・5段階評価ができる
   （評価はスタイルごとに集計され、次回のスタイル初期選択に自動で反映される）。
5. **⑤ 説明文だけ保存** — 全動画分のテキストをまとめてダウンロード。

## 技術構成

- `app.py` — Streamlit UI とアプリの状態管理
- `core.py` — Streamlit非依存のロジック（シーン検出、画像合成、ffmpeg合成、
  商品ページ取得、評価ログ）。単体テストしやすいよう分離してある
- Gemini呼び出しは `google-genai`（新SDK）を使用。モデルは `gemini-flash-lite-latest`
  （エイリアスなので将来のモデル更新にも追従する）
- シーン検出はOpenCVのフレーム差分ベースの簡易実装（`core.detect_scenes`）
- テキスト/スタンプ画像はPillowで生成し、ffmpegの`overlay`+`fade`フィルタで
  シーンの時間帯だけクロスフェード表示しながら合成する
- 評価ログは `feedback_log.jsonl`（JSON Lines）にアプリと同じフォルダへ追記される

## 注意

- `.streamlit/secrets.toml` にAPIキーを平文で保存している。このフォルダをgit管理する
  場合は `.gitignore` に `.streamlit/secrets.toml` を必ず追加すること
- フォントパスはWindows前提（`C:/Windows/Fonts/...`）。他OSに持ち出す場合は
  `core.py` の `FONT_OPTIONS` / `FONT_BOLD` を書き換える必要がある
- `.streamlit/config.toml` のテーマカラー（`primaryColor`）はBuyBeeの正式なブランド
  イエローではなく仮の値。ブランドカラーの正確な16進数が分かれば合わせて調整するとよい
