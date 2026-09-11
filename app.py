import streamlit as st
from google import genai
from google.genai import types
import cv2
import tempfile
import os
import json
import time
from datetime import datetime

from core import (
    STAMP_COLORS, STYLE_PRESETS, FONT_OPTIONS, TEXT_COLORS,
    detect_scenes, grab_frame_at, analyze_zones,
    build_text_overlay, build_stamp, stamp_target_position, burn_video_scenes,
    log_feedback, style_average_ratings, shuffle_stamp, notify_done, fetch_product_page_text,
)

st.set_page_config(page_title="BuyBee Stories 自動化", page_icon="🛋️", layout="centered")

# APP_PASSWORDが設定されている場合だけ簡易ゲートを表示する
# (公開デプロイ時に誰でもGemini APIのクォータ/料金を使えてしまうのを防ぐため。
# ローカル実行でsecretsに設定していなければ、このチェックは素通りする)。
APP_PASSWORD = st.secrets.get("APP_PASSWORD", "")
if APP_PASSWORD:
    if not st.session_state.get("authenticated"):
        st.title("🛋️ BuyBee Stories 自動生成")
        pw = st.text_input("パスワード", type="password")
        if st.button("入る", type="primary"):
            if pw == APP_PASSWORD:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("パスワードが違います")
        st.stop()

GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    st.error("⚠️ Gemini API キーが設定されていません")
    st.stop()

GEMINI_MODEL = "gemini-flash-lite-latest"
client = genai.Client(api_key=GEMINI_API_KEY)


@st.cache_data(ttl=3600)
def check_api_key():
    try:
        client.models.generate_content(model=GEMINI_MODEL, contents="ping")
        return True, ""
    except Exception as e:
        return False, str(e)


key_ok, key_error = check_api_key()
if not key_ok:
    st.error(
        "⚠️ Gemini APIキーが無効、またはモデルに接続できないため動画を分析できません。"
        "https://aistudio.google.com/apikey で新しいキーを発行し、"
        ".streamlit/secrets.toml の GEMINI_API_KEY を書き換えてください。"
    )
    st.caption(f"詳細: {key_error}")
    st.stop()


def ask_gemini(frame_path, style_key, product_context=""):
    style_desc = STYLE_PRESETS[style_key]
    with open(frame_path, "rb") as f:
        image_bytes = f.read()

    context_block = (
        f"\n参考: 以下は商品ページから取得した情報です。動画フレームの見た目と合わせて、"
        f"正確な情報(サイズ・価格・特徴)があればそちらを優先して使ってください。\n{product_context}\n"
        if product_context else ""
    )

    prompt = f"""これは中古家具リサイクルショップ「BuyBee」のInstagram/TikTok Stories用の動画フレームです。
テロップは次のスタイルで作成してください: {style_desc}
価格が分からない場合は price を空文字にしてください。体験談や断定的な効果効能は書かないでください。
{context_block}
以下のJSON形式で出力してください。

{{
  "title": "一番上に大きく出す煽り文句・キャッチコピー（10文字以内、例: 入荷しました！）",
  "body": "特徴を短く言い切る形で1〜3個（各10〜15文字、例: 訳あり！／キズあり！／高さ調節できる！）を\\nで改行区切りにした文字列",
  "price": "価格情報（分かる場合のみ、例: ￥6,600税込）",
  "hashtags": ["#家具", "#インテリア", "#リサイクル"],
  "stamp_text": "隅に貼るワンポイントの短い言葉（例: NEW, 訳あり, SALE）",
  "stamp_color": "ピンク/イエロー/レッド/グリーン/ブルー/オレンジ/パープルのいずれか"
}}
"""
    last_error = None
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[types.Part.from_bytes(data=image_bytes, mime_type="image/png"), prompt],
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            return json.loads(response.text)
        except Exception as e:
            last_error = e
            if attempt < 2:
                # 無料枠は1分あたりのリクエスト数制限があり、429(RESOURCE_EXHAUSTED)は
                # 短い待機では回復しないため長めに待つ。それ以外の一時的な503/400は
                # 短い待機で十分なことが多い。
                wait = 20 if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e) else 2 * (attempt + 1)
                time.sleep(wait)
    raise last_error


def analyze_video(video_path, style_key, product_context=""):
    scene_bounds = detect_scenes(video_path)
    scenes = []
    for start, end in scene_bounds:
        duration = end - start
        # 中央のフレームがまれに解析できない(破損/ブレ等)場合に備え、
        # 25%・75%地点のフレームも試してから諦める。
        candidate_times = [start + duration * r for r in (0.5, 0.25, 0.75)]

        text_zone = stamp_corner = None
        result, last_error = {}, None
        for t in candidate_times:
            ret, frame = grab_frame_at(video_path, t)
            if not ret:
                continue
            if text_zone is None:
                text_zone, stamp_corner = analyze_zones(frame)
            frame_temp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
            cv2.imwrite(frame_temp.name, frame)
            frame_temp.close()
            try:
                result = ask_gemini(frame_temp.name, style_key, product_context)
                last_error = None
                break
            except Exception as e:
                last_error = e
                # 無料枠は短時間の連続リクエストにも弱いため、次のフォールバック
                # フレームを試す前に少し間を空ける。
                time.sleep(3)
            finally:
                os.unlink(frame_temp.name)

        if last_error is not None:
            st.error(f"シーン分析に失敗しました: {last_error}")
        if text_zone is None:
            continue

        scenes.append({
            "start": start, "end": end,
            "text_zone": text_zone, "stamp_corner": stamp_corner,
            "text_scale": 1.0, "skip": False,
            "font_choice": list(FONT_OPTIONS.keys())[0], "text_color": "白",
            **result,
        })
    return scenes


def render_job(job):
    render_scenes = []
    for scene in job["scenes"]:
        if scene.get("skip"):
            continue
        font_path = FONT_OPTIONS.get(scene.get("font_choice"), list(FONT_OPTIONS.values())[0])
        text_color = TEXT_COLORS.get(scene.get("text_color"), "#FFFFFF")
        text_img = build_text_overlay(
            scene.get("title", ""), scene.get("body", ""), scene.get("price", ""),
            zone=scene["text_zone"], scale=scene.get("text_scale", 1.0),
            font_path=font_path, text_color=text_color,
        )
        stamp_img = build_stamp(
            scene.get("stamp_text", "NEW"), scene.get("stamp_color", "ピンク"),
            corner=scene["stamp_corner"], font_path=font_path,
        )
        stamp_xy = stamp_target_position(stamp_img, corner=scene["stamp_corner"], text_zone=scene["text_zone"])
        render_scenes.append({
            "start": scene["start"], "end": scene["end"],
            "text_img": text_img, "stamp_img": stamp_img, "stamp_xy": stamp_xy,
        })
    out_path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4").name
    burn_video_scenes(job["video_path"], render_scenes, out_path)
    return out_path


st.title("🛋️ BuyBee Stories 自動生成")
st.caption("動画をアップロードすると、シーンごとにGeminiが説明文とスタンプを生成し、Stories用の動画に焼き込みます。複数本まとめて処理できます。")

with st.container(border=True):
    st.subheader("① 動画をアップロード（複数可）")
    uploaded_files = st.file_uploader(
        "mp4 / mov / avi", type=["mp4", "mov", "avi"],
        accept_multiple_files=True, label_visibility="collapsed",
    )

if uploaded_files:
    upload_names = [f.name for f in uploaded_files]
    if st.session_state.get("job_names") != upload_names:
        # 既に分析済みの動画は保持し、ファイルの追加/削除があった分だけ更新する
        # (アップロード欄をいじるたびに全部の分析結果が消えるのを防ぐ)。
        existing_by_name = {job["name"]: job for job in st.session_state.get("jobs", [])}
        jobs = []
        for i, f in enumerate(uploaded_files):
            if f.name in existing_by_name:
                job = existing_by_name[f.name]
            else:
                temp_video = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
                temp_video.write(f.read())
                temp_video.close()
                job = {
                    "name": f.name, "video_path": temp_video.name,
                    "scenes": None, "style_key": None,
                    "output_path": None, "saved_rating_for": None,
                }
            job["id"] = i
            jobs.append(job)
        st.session_state["jobs"] = jobs
        st.session_state["job_names"] = upload_names

    jobs = st.session_state["jobs"]
    color_names = list(STAMP_COLORS.keys())

    with st.container(border=True):
        st.subheader("② テロップの雰囲気を選んで分析")
        style_options = list(STYLE_PRESETS.keys())
        ratings = style_average_ratings()
        default_index = 0
        if ratings:
            best_style = max(ratings.items(), key=lambda kv: kv[1][0])[0]
            if best_style in style_options:
                default_index = style_options.index(best_style)
            stats_line = " / ".join(f"{name}: 平均{avg:.1f}点(n={n})" for name, (avg, n) in ratings.items())
            st.caption(f"過去の評価（自動で反映済み）: {stats_line}")

        style_key = st.selectbox("スタイル", style_options, index=default_index)

        st.caption("商品ページのURLを貼ると、動画の見た目に加えてページの情報も参考にして生成します（任意）")
        for job in jobs:
            job["product_url"] = st.text_input(
                f"商品URL（任意）— {job['name']}",
                value=job.get("product_url", ""), key=f"url_{job['id']}",
            )

        if st.button(f"🔍 {len(jobs)}本を分析する", type="primary", use_container_width=True):
            with st.spinner(f"{len(jobs)}本のシーンを検出し、Geminiが分析中..."):
                for job in jobs:
                    product_context = ""
                    url = job.get("product_url", "").strip()
                    if url:
                        try:
                            product_context = fetch_product_page_text(url)
                        except Exception as e:
                            st.warning(f"「{job['name']}」の商品URL取得に失敗したため、動画のみで分析します: {e}")
                    job["scenes"] = analyze_video(job["video_path"], style_key, product_context)
                    job["style_key"] = style_key
                    job["output_path"] = None
                    job["saved_rating_for"] = None

    if all(job["scenes"] is not None for job in jobs):
        with st.container(border=True):
            st.subheader("③ 動画ごとに内容を確認・編集")
            for job in jobs:
                with st.expander(f"🎬 {job['name']}（シーン{len(job['scenes'])}件）", expanded=True):
                    scenes = job["scenes"]
                    tabs = st.tabs([f"シーン{i + 1}（{s['start']:.1f}s〜{s['end']:.1f}s）" for i, s in enumerate(scenes)])
                    for i, (tab, scene) in enumerate(zip(tabs, scenes)):
                        with tab:
                            key_prefix = f"{job['id']}_{i}"
                            scene["skip"] = st.checkbox(
                                "このシーンにはテロップ・スタンプを入れない",
                                value=scene.get("skip", False), key=f"skip_{key_prefix}",
                            )
                            disabled = scene["skip"]
                            scene["title"] = st.text_input(
                                "キャッチコピー", value=scene.get("title", ""),
                                key=f"title_{key_prefix}", disabled=disabled,
                            )
                            scene["body"] = st.text_area(
                                "特徴（改行で複数行）", value=scene.get("body", ""), height=100,
                                key=f"body_{key_prefix}", disabled=disabled,
                            )
                            scene["price"] = st.text_input(
                                "価格", value=scene.get("price", ""),
                                key=f"price_{key_prefix}", disabled=disabled,
                            )
                            raw_hashtags = scene.get("hashtags", "")
                            hashtags_value = " ".join(raw_hashtags) if isinstance(raw_hashtags, list) else raw_hashtags
                            scene["hashtags"] = st.text_input(
                                "ハッシュタグ", value=hashtags_value, key=f"hashtags_{key_prefix}",
                            )
                            scene["text_scale"] = st.slider(
                                "文字の大きさ", 0.6, 1.6, value=scene.get("text_scale", 1.0), step=0.1,
                                key=f"scale_{key_prefix}", disabled=disabled,
                            )
                            font_names = list(FONT_OPTIONS.keys())
                            color_choice_names = list(TEXT_COLORS.keys())
                            fcol1, fcol2 = st.columns(2)
                            with fcol1:
                                default_font = scene.get("font_choice", font_names[0])
                                scene["font_choice"] = st.selectbox(
                                    "フォント", font_names,
                                    index=font_names.index(default_font) if default_font in font_names else 0,
                                    key=f"font_{key_prefix}", disabled=disabled,
                                )
                            with fcol2:
                                default_text_color = scene.get("text_color", "白")
                                scene["text_color"] = st.selectbox(
                                    "文字の色", color_choice_names,
                                    index=color_choice_names.index(default_text_color) if default_text_color in color_choice_names else 0,
                                    key=f"textcolor_{key_prefix}", disabled=disabled,
                                )
                            col1, col2, col3 = st.columns([2, 1, 1])
                            with col1:
                                scene["stamp_text"] = st.text_input(
                                    "スタンプ文言", value=scene.get("stamp_text", "NEW"),
                                    key=f"stamp_text_{key_prefix}", disabled=disabled,
                                )
                            with col2:
                                default_color = scene.get("stamp_color", "ピンク")
                                scene["stamp_color"] = st.selectbox(
                                    "スタンプ色", color_names,
                                    index=color_names.index(default_color) if default_color in color_names else 0,
                                    key=f"stamp_color_{key_prefix}", disabled=disabled,
                                )
                            with col3:
                                st.write("")
                                if st.button("🔄 再生成", key=f"regen_{key_prefix}", disabled=disabled, use_container_width=True):
                                    new_text, new_color = shuffle_stamp(
                                        scene.get("stamp_text", "NEW"), scene.get("stamp_color", "ピンク"), color_names,
                                    )
                                    scene["stamp_text"], scene["stamp_color"] = new_text, new_color
                                    st.rerun()

        with st.container(border=True):
            st.subheader("④ 動画を生成")
            if st.button(f"🎬 {len(jobs)}本まとめて焼き込む", type="primary", use_container_width=True):
                progress = st.progress(0.0)
                for n, job in enumerate(jobs):
                    with st.spinner(f"「{job['name']}」を書き出し中... ({n + 1}/{len(jobs)})"):
                        try:
                            job["output_path"] = render_job(job)
                            job["saved_rating_for"] = None
                            notify_done("BuyBee Stories", f"「{job['name']}」の動画が完成しました")
                        except Exception as e:
                            st.error(f"「{job['name']}」の生成に失敗しました: {e}")
                    progress.progress((n + 1) / len(jobs))
                notify_done("BuyBee Stories", f"{len(jobs)}本すべての書き出しが完了しました")
                st.success(f"✅ {len(jobs)}本の書き出しが完了しました")

            for job in jobs:
                if not job.get("output_path"):
                    continue
                st.markdown(f"##### 🎬 {job['name']}")
                st.video(job["output_path"])
                with open(job["output_path"], "rb") as f:
                    st.download_button(
                        f"📥「{job['name']}」をダウンロード", data=f.read(),
                        file_name=f"buybee_story_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{job['id']}.mp4",
                        mime="video/mp4", use_container_width=True, key=f"dl_{job['id']}",
                    )

                st.caption("この動画を5段階で評価してください（次回のスタイルの初期選択に反映されます。評価後も上の内容は編集できます）")
                stars = st.feedback("stars", key=f"stars_{job['id']}")
                already_saved = job.get("saved_rating_for") == job["output_path"]
                if stars is not None and not already_saved:
                    if st.button("📝 この評価を保存する", key=f"save_rating_{job['id']}", use_container_width=True):
                        log_feedback({
                            "timestamp": datetime.now().isoformat(),
                            "style": job.get("style_key"),
                            "rating": stars + 1,
                            "scene_count": len(job["scenes"]),
                            "stamp_colors": [s.get("stamp_color") for s in job["scenes"]],
                            "text_zones": [s.get("text_zone") for s in job["scenes"]],
                        })
                        job["saved_rating_for"] = job["output_path"]
                        st.success("評価を保存しました。")
                elif already_saved:
                    st.caption("この動画の評価は保存済みです。")
                st.divider()

        with st.container(border=True):
            st.subheader("⑤ 説明文だけ保存")
            all_parts = []
            for job in jobs:
                parts = [f"=== {job['name']} ==="]
                for i, scene in enumerate(job["scenes"]):
                    if scene.get("skip"):
                        continue
                    parts.append(
                        f"[シーン{i + 1}]\n{scene.get('title', '')}\n{scene.get('body', '')}\n"
                        f"{scene.get('price', '')}\n{scene.get('hashtags', '')}"
                    )
                all_parts.append("\n\n".join(parts))
            full_text = "\n\n\n".join(all_parts)
            st.download_button(
                "📥 説明文をテキストで保存（全動画分）", data=full_text,
                file_name=f"buybee_description_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
                mime="text/plain", use_container_width=True,
            )
