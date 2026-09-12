import os
import io
import json
import time
import uuid
import shutil
import tempfile
import threading

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types
import cv2

from core import (
    STAMP_COLORS, STYLE_PRESETS, FONT_OPTIONS, TEXT_COLORS,
    detect_scenes, grab_frame_at, analyze_zones,
    build_text_overlay, build_stamp, stamp_target_position, burn_video_scenes,
    log_feedback, style_average_ratings, shuffle_stamp, fetch_product_page_text,
)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-flash-lite-latest"
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

app = FastAPI()

JOBS = {}
JOBS_LOCK = threading.Lock()


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
価格が分からない場合は price を空文字にしてください。体験談や断定的な効果効能は書かないでください。
{context_block}
以下のJSON形式で出力してください。

{{
  "title": "一番上に大きく出す煽り文句・キャッチコピー（10文字以内、例: 入荷しました！）",
  "body": "特徴を短く言い切る形で1〜3個（各10〜15文字、例: 訳あり！／キズあり！／高さ調節できる！）を\\nで改行区切りにした文字列",
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
                config=types.GenerateContentConfig(response_mime_type="application/json"),
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


def analyze_video_job(job_id, style_key, product_url):
    job = JOBS[job_id]
    try:
        product_context = ""
        if product_url:
            try:
                product_context = fetch_product_page_text(product_url)
            except Exception as e:
                job["warning"] = f"商品URL取得に失敗したため、動画のみで分析します: {e}"

        video_path = job["video_path"]
        scene_bounds = detect_scenes(video_path)
        scenes = []
        for idx, (start, end) in enumerate(scene_bounds):
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
                    last_error = None
                    break
                except Exception as e:
                    last_error = e
                    time.sleep(3)
                finally:
                    os.unlink(frame_temp.name)
            if last_error is not None:
                job["warning"] = f"シーン分析に失敗しました: {last_error}"
            if text_zone is None:
                continue
            scenes.append({
                "start": start, "end": end,
                "text_zone": text_zone, "stamp_corner": stamp_corner,
                "text_scale": 1.0, "skip": False,
                "font_choice": list(FONT_OPTIONS.keys())[0], "text_color": "白",
                **result,
            })

        job["scenes"] = scenes
        job["style_key"] = style_key
        job["status"] = "analyzed"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


def render_job_task(job_id):
    job = JOBS[job_id]
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
        burn_video_scenes(job["video_path"], render_scenes, out_path)
        job["output_path"] = out_path
        job["status"] = "done"
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
    }


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
            "output_path": None, "error": None, "warning": None,
        }
        created.append({"id": job_id, "name": f.filename})
    return {"jobs": created}


@app.post("/api/jobs/{job_id}/analyze")
def analyze(job_id: str, style_key: str = Form(...), product_url: str = Form("")):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    job["status"] = "analyzing"
    job["warning"] = None
    threading.Thread(target=analyze_video_job, args=(job_id, style_key, product_url), daemon=True).start()
    return {"status": "analyzing"}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return {
        "id": job["id"], "name": job["name"], "status": job["status"],
        "scenes": job["scenes"], "error": job["error"], "warning": job["warning"],
        "has_output": bool(job["output_path"]),
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
    threading.Thread(target=render_job_task, args=(job_id,), daemon=True).start()
    return {"status": "rendering"}


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
