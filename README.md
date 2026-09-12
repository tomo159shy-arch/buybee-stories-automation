# BuyBee Stories 自動化

家具の動画（複数本まとめてOK）をアップロードすると、シーン（カット）ごとにGeminiが
キャッチコピー・特徴・価格・ハッシュタグ・ワンポイントスタンプを生成し、
Stories用の縦動画（9:16）に自然なクロスフェードで焼き込んで書き出すWebアプリ。

フロントエンドは[在庫管理アプリ](https://buybee-inventory.vercel.app)と同じApple HIGトークン
（色・角丸・タイポグラフィ）を使った完全自作HTML/CSS/JS。バックエンドはFastAPI。

## セットアップ（ローカル）

```bash
cd buybee_stories_automation
pip install -r requirements.txt
set GEMINI_API_KEY=あなたのGeminiAPIキー   (PowerShellなら $env:GEMINI_API_KEY="...")
uvicorn server:app --reload
```

ブラウザで http://localhost:8000 を開く。ffmpegがPATHに必要（`ffmpeg -version` で確認）。

## 使い方

1. **動画をアップロード** — mp4/mov/avi、複数選択可。
2. **テロップの雰囲気を選ぶ** — スタイル（煽り系/かわいい系/情報系）をタップ。
   動画ごとに商品ページのURLを貼ると、そのページの情報も加味して生成する（任意）。
   下部の「分析する」を押すとシーンを自動検出し、シーンごとにGeminiが内容を生成する。
3. **シーンごとに内容を確認・編集** — シーンタブを切り替えながら、キャッチコピー・特徴・
   価格・ハッシュタグ・文字の大きさ・フォント・文字の色・スタンプ文言・スタンプ色を編集できる。
   🔄でスタンプ文言・色だけをAPIを使わず即座にシャッフルできる。
   「このシーンには入れない」チェックでそのシーンだけテロップ・スタンプを外せる。
4. **動画を生成** — 下部のボタンでまとめて書き出し。完成した動画はその場でプレビュー・
   ダウンロード・5段階評価ができる（評価はスタイルごとに集計され、次回の
   スタイル初期選択に自動で反映される）。
5. **説明文だけ保存** — 全動画分のテキストをまとめてダウンロード（ブラウザ内で生成、通信不要）。

## 技術構成

- `static/index.html` — フロントエンド一式（HTML/CSS/JS、ビルド不要）
- `server.py` — FastAPIバックエンド。アップロード・分析・レンダリングをジョブ単位で
  管理し、重い処理（Gemini呼び出し・ffmpeg合成）はバックグラウンドスレッドで実行、
  フロントは短い間隔でポーリングして進捗を見る
- `core.py` — Streamlit時代から引き継いだ非フレームワーク依存のロジック
  （シーン検出、画像合成、ffmpeg合成、商品ページ取得、評価ログ）
- Gemini呼び出しは `google-genai`（新SDK）。モデルは `gemini-flash-lite-latest`
  （エイリアスなので将来のモデル更新にも追従する）
- シーン検出はOpenCVのフレーム差分ベースの簡易実装（`core.detect_scenes`）
- テキスト/スタンプ画像はPillowで生成し、ffmpegの`overlay`+`fade`フィルタで
  シーンの時間帯だけクロスフェード表示しながら合成する
- 評価ログは `feedback_log.jsonl`（JSON Lines）にサーバーと同じフォルダへ追記される
  （デプロイ環境によっては再デプロイで消える点に注意）

## デプロイ

Vercelのようなサーバーレスはffmpegの実行時間・ファイルサイズ制限と相性が悪いため、
`Dockerfile`を使ってRender等の「常駐できるPythonサーバー」向けにビルドする想定。

1. GitHubにpush
2. Render（render.com）で「New +」→「Blueprint」→ このリポジトリを選択
   （`render.yaml`を自動検出）
3. 環境変数 `GEMINI_API_KEY` を設定してDeploy

## 注意

- フォントはWindowsパス（`C:/Windows/Fonts/...`）を優先し、無ければLinuxの
  Noto CJK（`fonts-noto-cjk`、Dockerfileで導入済み）にフォールバックする
  （`core.py` の `_first_existing`）
- ジョブの状態はサーバープロセスのメモリ上に保持している（再起動で消える、
  複数インスタンスには非対応）。小規模な社内ツール利用を想定した割り切り
