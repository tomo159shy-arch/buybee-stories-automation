import os
import io
import re
import json
import time
import uuid
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types
from pywebpush import webpush, WebPushException
import cv2

from core import (
    STAMP_COLORS, STYLE_PRESETS, FONT_OPTIONS, TEXT_COLORS,
    detect_scenes, grab_frame_at, analyze_zones, probe_duration,
    build_text_overlay, build_stamp, stamp_target_position, burn_video_scenes,
    log_feedback, style_average_ratings, shuffle_stamp, fetch_product_page_text,
)
import gcs_store

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-flash-lite-latest"
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_CLAIM_EMAIL = os.environ.get("VAPID_CLAIM_EMAIL", "mailto:admin@example.com")
# Cloud Runはインスタンスが再起動すると空メモリに戻ってしまうため、
# 購読情報はGCSから読み込んで復元する(でないと再起動ごとに
# 「通知が来ない」状態になる)。
PUSH_SUBSCRIPTIONS = gcs_store.load_push_subscriptions()
PUSH_LOCK = threading.Lock()

app = FastAPI()

JOBS = {}
JOBS_LOCK = threading.Lock()


def send_push_to_all(title, body):
    """完成通知をすべての購読者に送る。無効になった購読は取り除く。"""
    if not VAPID_PRIVATE_KEY:
        return
    dead = []
    with PUSH_LOCK:
        subs = list(PUSH_SUBSCRIPTIONS)
    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps({"title": title, "body": body}),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_CLAIM_EMAIL},
            )
        except WebPushException:
            dead.append(sub)
        except Exception:
            dead.append(sub)
    if dead:
        with PUSH_LOCK:
            for sub in dead:
                if sub in PUSH_SUBSCRIPTIONS:
                    PUSH_SUBSCRIPTIONS.remove(sub)
            subs_copy = list(PUSH_SUBSCRIPTIONS)
        try:
            gcs_store.save_push_subscriptions(subs_copy)
        except Exception:
            pass


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
主役は必ず画面に写っている家具そのものです。人物や手が映っていても、それは家具の使い方を
見せているだけなので、テロップは家具本体の特徴(種類・素材・色・サイズ感・触り心地・機能、
例:収納付き/伸縮式/クッション性など)を中心に書いてください。家具と無関係な行動の説明や、
家具の種類を無視した抽象的な言い回しは避けてください。
テロップは次のスタイルで作成してください: {style_desc}

実際のBuyBeeの投稿の文体はこんな感じです(参考。そのまま使わず、写っている家具に
合わせて書き換えること):
「くつろぎ空間を上質に演出する♡」「すっきり細脚で圧迫感を軽減」
「高さ調節できて使いやすい／シーンに合わせて自由に昇降」「丸いガラスが優しく灯る照明」
「訳あり！あげる時少し硬い😭」
→ 「特徴の言い切りを箇条書き」ではなく、家具の魅力や使い心地が伝わる自然な一文
(絵文字やハートは0〜1個までで控えめに)。硬い宣伝文句よりも、店員が実際に見て
思ったことをそのまま書いたような、少し肩の力が抜けた言い回しにしてください。

画面内に価格を書いた値札・POP(紙やカード)が写っていることが多いので、見えていたら
そこに書かれた数字を正確に読み取ってpriceに入れてください（税込/税抜の記載があれば
それも含める）。見えない・読み取れない場合のみ price を空文字にしてください。
体験談や断定的な効果効能は書かないでください。
{context_block}
以下のJSON形式で出力してください。

{{
  "title": "一番上に大きく出す煽り文句・キャッチコピー（10文字以内、例: 入荷しました！／NEW）",
  "body": "家具の魅力・使い心地を伝える自然な一文、または関連する2行以内のフレーズ（各行10〜18文字程度、\\nで改行区切り。例: くつろぎ空間を上質に演出する♡ / すっきり細脚で圧迫感を軽減）",
  "price": "価格情報（分かる場合のみ、例: ￥6,600税込）",
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
                # timeoutを指定しないとAPI側がハングした場合に無期限に待ち続けて
                # しまう(「分析が進まない」不具合の再発防止)。ミリ秒指定。
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    http_options=types.HttpOptions(timeout=30_000),
                ),
            )
            parsed = json.loads(response.text)
            # まれにGeminiが単一オブジェクトではなく配列で返すことがあるため、
            # その場合は先頭要素を採用し、辞書でなければリトライさせる。
            if isinstance(parsed, list):
                parsed = parsed[0] if parsed else {}
            if not isinstance(parsed, dict):
                raise ValueError(f"予期しない形式のレスポンス: {type(parsed).__name__}")
            return parsed
        except Exception as e:
            last_error = e
            if attempt < 2:
                wait = 20 if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e) else 2 * (attempt + 1)
                time.sleep(wait)
    raise last_error


def analyze_one_scene(job, idx, start, end, style_key, product_context, font_choice, text_color, frame_source_path):
    """1シーン分の代表フレーム抽出+Gemini分析。シーン間で並列実行できるよう
    副作用(ファイル保存)以外は全部この関数の中で完結させている。
    frame_source_path: 値札等の文字を正確に読めるよう、縮小前の元動画を渡す。"""
    video_path = frame_source_path
    duration = end - start
    candidate_times = [start + duration * r for r in (0.5, 0.25, 0.75)]
    text_zone = stamp_corner = None
    result, last_error = {}, None
    for t in candidate_times:
        ret, frame = grab_frame_at(video_path, t)
        if not ret:
            continue
        if text_zone is None:
            text_zone, stamp_corner = analyze_zones(frame)
            # 編集画面でどのシーンか目で見て確認できるよう保存しておく
            cv2.imwrite(os.path.join(job["job_dir"], f"scene_{idx}.jpg"), frame)
        frame_temp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        cv2.imwrite(frame_temp.name, frame)
        frame_temp.close()
        try:
            result = ask_gemini(frame_temp.name, style_key, product_context)
            if not result.get("title") and not result.get("body"):
                # APIは成功したが中身が空。原因が見えなくなるので失敗扱いにして
                # 次の候補フレームを試す/最終的に警告を出す。
                raise ValueError("生成結果が空でした")
            last_error = None
            break
        except Exception as e:
            last_error = e
            time.sleep(3)
        finally:
            os.unlink(frame_temp.name)

    if text_zone is None:
        return None, last_error
    scene = {
        "start": start, "end": end,
        "text_zone": text_zone, "stamp_corner": stamp_corner,
        "text_scale": 1.0, "skip": False,
        "font_choice": font_choice or list(FONT_OPTIONS.keys())[0],
        "text_color": text_color or "白",
        **result,
    }
    return scene, last_error


def analyze_video_job(job_id, style_key, product_url, font_choice, text_color):
    job = JOBS[job_id]
    job["progress"] = 5
    try:
        product_context = ""
        if product_url:
            try:
                product_context = fetch_product_page_text(product_url)
            except Exception as e:
                job["warning"] = f"商品URL取得に失敗したため、動画のみで分析します: {e}"

        # MAX_SCENES=1(動画全体を通して同じテキスト1つ)にしたので、
        # detect_scenesは実際にはフレームをスキャンせず、動画の長さだけ
        # 読んで即座に「動画全体=1シーン」を返す(core.detect_scenes参照)。
        # そのため軽量プローブ作成やシーン分割スキャンはそもそも不要。
        # それでも将来MAX_SCENESを増やす場合に備え、タイムアウトの安全装置
        # (「5%のまま進まない」不具合の原因だった)はそのまま残しておく。
        original_path = job["video_path"]
        probe_pool = ThreadPoolExecutor(max_workers=1)
        try:
            scene_bounds = probe_pool.submit(detect_scenes, original_path).result(timeout=30)
        except FuturesTimeoutError:
            duration = probe_duration(original_path) or 10.0
            scene_bounds = [(0.0, duration)]
            job["warning"] = (job.get("warning") or "") + " シーン検出が時間内に終わらなかったため、動画全体を1シーンとして扱いました"
        finally:
            probe_pool.shutdown(wait=False)
        job["progress"] = 15

        # シーンごとのGemini呼び出しは互いに独立しているので並列に実行して
        # 分析時間を短縮する(逐次だとシーン数×待ち時間がそのまま積み上がる)。
        # 完了したシーンの数に応じて15%→50%まで進捗を進める。
        total_scenes = max(len(scene_bounds), 1)
        done_count = 0
        results = [None] * len(scene_bounds)
        with ThreadPoolExecutor(max_workers=max(len(scene_bounds), 1)) as pool:
            futures = {
                pool.submit(
                    analyze_one_scene, job, idx, start, end, style_key, product_context,
                    font_choice, text_color, original_path,
                ): idx
                for idx, (start, end) in enumerate(scene_bounds)
            }
            for future in as_completed(futures):
                idx = futures[future]
                results[idx] = future.result()
                done_count += 1
                job["progress"] = 15 + int(35 * done_count / total_scenes)

        scenes = []
        warnings = []
        for scene, last_error in results:
            if last_error is not None:
                warnings.append(str(last_error))
            if scene is not None:
                # フレーム取得自体は成功したが、Gemini分析が(リトライも含め)
                # 全て失敗し、title/bodyが1つも取れなかった場合。このまま
                # 書き出すとテキストが空欄のまま動画だけ完成してしまうので、
                # 「テロップなしでそのまま動画を使う」扱いに切り替える。
                if not scene.get("title") and not scene.get("body"):
                    scene["skip"] = True
                scenes.append(scene)
        if warnings:
            job["warning"] = "シーン分析に失敗しました: " + " / ".join(warnings)

        # 最終書き出し(burn_video_scenes)は元動画を直接使う。そちら自身の
        # scaleフィルタでOUT_W/OUT_Hに縮小されるので、ここで別途プロキシを
        # 作ると二重にffmpeg変換することになり遅くなるだけ。Cloud Run
        # (2GB/2CPU)ならこの1回の変換で十分間に合う。
        job["video_path"] = original_path

        job["scenes"] = scenes
        job["style_key"] = style_key
        job["status"] = "analyzed"
        job["progress"] = 55
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        return

    # 分析が終わったらブラウザを待たずそのまま書き出しまで進める
    # (画面を閉じていても最後まで完了させたいため、連携はサーバー側で行う)。
    render_job_task(job_id)


def render_job_task(job_id):
    job = JOBS[job_id]
    job["progress"] = max(job.get("progress") or 0, 55)
    try:
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
        out_path = os.path.join(job["job_dir"], "output.mp4")

        def _on_progress(frac):
            # 書き出し本編は55%~95%の範囲にマッピングする。
            job["progress"] = 55 + int(frac * 40)

        burn_video_scenes(job["video_path"], render_scenes, out_path, progress_cb=_on_progress)
        job["output_path"] = out_path
        job["status"] = "done"
        job["progress"] = 95

        # Cloud Runのローカルディスクはインスタンスが再起動すると消えるため、
        # 完成した動画は必ずGCS(永続ストレージ)にもアップロードしておく。
        # これが無いと「完成通知が来た頃には見れなくなっている」が起きる。
        try:
            safe_name = re.sub(r"[^\w.\-]+", "_", os.path.splitext(job["name"])[0])[:60]
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            object_name = f"{ts}_{safe_name}_{job_id[:8]}.mp4"
            gcs_store.upload_video(out_path, object_name)
            job["library_name"] = object_name
        except Exception as e:
            job["warning"] = (job.get("warning") or "") + f" 動画の永続保存に失敗しました: {e}"

        job["progress"] = 100
        send_push_to_all("BuyBee Stories", f"「{job['name']}」の動画が完成しました")
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.get("/api/options")
def get_options():
    return {
        "styles": list(STYLE_PRESETS.keys()),
        "fonts": list(FONT_OPTIONS.keys()),
        "text_colors": [{"name": k, "hex": v} for k, v in TEXT_COLORS.items()],
        "stamp_colors": [{"name": k, "hex": v} for k, v in STAMP_COLORS.items()],
        "style_ratings": [
            {"name": name, "avg": avg, "n": n}
            for name, (avg, n) in style_average_ratings().items()
        ],
        "push_enabled": bool(VAPID_PRIVATE_KEY),
    }


@app.get("/api/push/public-key")
def push_public_key():
    return {"publicKey": VAPID_PUBLIC_KEY}


@app.post("/api/push/subscribe")
async def push_subscribe(payload: dict):
    with PUSH_LOCK:
        if payload not in PUSH_SUBSCRIPTIONS:
            PUSH_SUBSCRIPTIONS.append(payload)
        subs_copy = list(PUSH_SUBSCRIPTIONS)
    # 次にインスタンスが再起動しても購読が消えないよう、その都度GCSに保存する。
    try:
        gcs_store.save_push_subscriptions(subs_copy)
    except Exception:
        pass
    return {"ok": True}


@app.get("/api/library")
def list_library():
    """今まで完成した動画の一覧(新しい順)。インスタンス再起動やブラウザの
    ジョブ一覧が消えても、ここからいつでも見返せる。"""
    try:
        return {"videos": gcs_store.list_videos()}
    except Exception as e:
        raise HTTPException(500, f"一覧の取得に失敗しました: {e}")


@app.get("/api/library/{object_name}/video")
def get_library_video(object_name: str):
    blob = gcs_store.get_video_blob(object_name)
    if blob is None or not blob.exists():
        raise HTTPException(404, "video not found")
    data = blob.download_as_bytes()
    return StreamingResponse(
        io.BytesIO(data), media_type="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="{object_name}"'},
    )


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    if client is None:
        raise HTTPException(500, "GEMINI_API_KEY が設定されていません")
    created = []
    for f in files:
        job_id = uuid.uuid4().hex
        job_dir = tempfile.mkdtemp(prefix=f"bb_{job_id}_")
        video_path = os.path.join(job_dir, f.filename)
        with open(video_path, "wb") as out:
            shutil.copyfileobj(f.file, out)
        JOBS[job_id] = {
            "id": job_id, "name": f.filename, "video_path": video_path, "job_dir": job_dir,
            "status": "uploaded", "scenes": None, "style_key": None,
            "output_path": None, "error": None, "warning": None, "progress": 0,
        }
        created.append({"id": job_id, "name": f.filename})
    return {"jobs": created}


@app.post("/api/jobs/{job_id}/analyze")
def analyze(
    job_id: str, style_key: str = Form(...), product_url: str = Form(""),
    font_choice: str = Form(""), text_color: str = Form(""),
):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    job["status"] = "analyzing"
    job["warning"] = None
    # Cloud Runはリクエスト処理外のバックグラウンドスレッドにCPUを
    # ちゃんと割り当てない場合があり、「分析中のまま進まない」原因になって
    # いた。このリクエストの中で(書き出しまで)同期的に処理を終わらせる
    # ことで、Cloud Run側にもこの処理時間分のCPUを確実に割り当てさせる。
    # 画面を閉じてもサーバー側の処理自体は最後まで続く。
    analyze_video_job(job_id, style_key, product_url, font_choice, text_color)
    return {"status": job["status"]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return {
        "id": job["id"], "name": job["name"], "status": job["status"],
        "scenes": job["scenes"], "error": job["error"], "warning": job["warning"],
        "has_output": bool(job["output_path"]), "progress": job.get("progress", 0),
    }


@app.post("/api/jobs/{job_id}/scenes")
async def update_scenes_body(job_id: str, payload: dict):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    job["scenes"] = payload.get("scenes", job["scenes"])
    return {"ok": True}


@app.post("/api/jobs/{job_id}/render")
def render(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if not job.get("scenes"):
        raise HTTPException(400, "scenes not analyzed yet")
    job["status"] = "rendering"
    render_job_task(job_id)
    return {"status": job["status"]}


@app.get("/api/jobs/{job_id}/video")
def get_video(job_id: str):
    job = JOBS.get(job_id)
    if not job or not job.get("output_path"):
        raise HTTPException(404, "video not ready")
    return FileResponse(job["output_path"], media_type="video/mp4", filename=f"buybee_story_{job['name']}.mp4")


@app.get("/api/jobs/{job_id}/scene/{index}/frame")
def get_scene_frame(job_id: str, index: int):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    frame_path = os.path.join(job["job_dir"], f"scene_{index}.jpg")
    if not os.path.exists(frame_path):
        raise HTTPException(404, "frame not found")
    return FileResponse(frame_path, media_type="image/jpeg")


@app.post("/api/jobs/{job_id}/rate")
async def rate(job_id: str, payload: dict):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    log_feedback({
        "timestamp": time.time(),
        "style": job.get("style_key"),
        "rating": payload.get("rating"),
        "scene_count": len(job.get("scenes") or []),
        "stamp_colors": [s.get("stamp_color") for s in (job.get("scenes") or [])],
        "text_zones": [s.get("text_zone") for s in (job.get("scenes") or [])],
    })
    return {"ok": True}


@app.post("/api/stamp/shuffle")
async def stamp_shuffle(payload: dict):
    color_names = list(STAMP_COLORS.keys())
    text, color = shuffle_stamp(payload.get("text", "NEW"), payload.get("color", "ピンク"), color_names)
    return {"text": text, "color": color}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
