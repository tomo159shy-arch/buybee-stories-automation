import tempfile
import os
import json
import random
import re
import subprocess
import threading
import numpy as np
import cv2
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

def _first_existing(*paths):
    """候補パスのうち最初に実在するものを返す(Windowsローカル実行と
    Streamlit Cloud(Linux)デプロイの両方で同じコードが動くようにするため)。
    どれも無ければ最後の候補をそのまま返し、フォント読み込み時のエラーで気付けるようにする。"""
    for p in paths:
        if os.path.exists(p):
            return p
    return paths[-1]


FONT_BOLD = _first_existing(
    "C:/Windows/Fonts/YuGothB.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
)
FONT_REGULAR = _first_existing(
    "C:/Windows/Fonts/YuGothM.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
)

FONT_OPTIONS = {
    "Yuゴシック太字（Instagram Classic風・標準）": FONT_BOLD,
    "メイリオ（がっちり太字）": _first_existing(
        "C:/Windows/Fonts/meiryob.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    ),
    "MSゴシック（レトロ・POP風）": _first_existing(
        "C:/Windows/Fonts/msgothic.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    ),
}

TEXT_COLORS = {
    "白": "#FFFFFF",
    "黒": "#111111",
    "イエロー": "#FFD60A",
    "ピンク": "#FF3D8A",
    "レッド": "#FF3B30",
    "ブルー": "#0A84FF",
}

STAMP_TEXT_POOL = ["NEW", "SALE", "訳あり", "限定", "お得", "入荷", "必見"]


def shuffle_stamp(current_text, current_color, color_names):
    """Gemini呼び出しなしで、今と違うスタンプ文言・色をランダムに選び直す
    (「スタンプの再生成」ボタン用。APIコストも掛けず即座に切り替えられる)。"""
    text_choices = [t for t in STAMP_TEXT_POOL if t != current_text] or STAMP_TEXT_POOL
    color_choices = [c for c in color_names if c != current_color] or color_names
    return random.choice(text_choices), random.choice(color_choices)



STAMP_COLORS = {
    "ピンク": "#FF3D8A",
    "イエロー": "#FFD60A",
    "レッド": "#FF3B30",
    "グリーン": "#34C759",
    "ブルー": "#0A84FF",
    "オレンジ": "#FF9F0A",
    "パープル": "#AF52DE",
}

STYLE_PRESETS = {
    "煽り・言い切り系（訳あり！キズあり！）": (
        "背景の枠を使わず、太字の白文字（縁取りあり）を写真に直接乗せる勢いのあるポップなスタイル。"
        "訳あり品なら「訳あり！あげる時少し硬い😭」のように、欠点を正直に、少し笑える"
        "人間くさいコメントとして言い切る（絵文字は1個まで）。傷や欠点が無い商品なら"
        "「即決おすすめ！」のような勢いのある一言で。"
    ),
    "かわいい系（絵文字・ハート）": (
        "絵文字やハート(♡)を交えた、やわらかく可愛い言い回し。"
        "「くつろぎ空間を上質に演出する♡」のような雰囲気。"
    ),
    "情報系（スペック重視）": (
        "煽らず、サイズ・素材・価格などのスペック情報を淡々と伝える。誇張表現・絵文字は避ける。"
    ),
}

FEEDBACK_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feedback_log.jsonl")


def log_feedback(record):
    with open(FEEDBACK_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_feedback():
    if not os.path.exists(FEEDBACK_LOG_PATH):
        return []
    records = []
    with open(FEEDBACK_LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def style_average_ratings():
    """スタイルプリセットごとの平均評価と件数を返す。{style: (avg, n)}"""
    stats = {}
    for r in load_feedback():
        style, rating = r.get("style"), r.get("rating")
        if style is None or rating is None:
            continue
        s = stats.setdefault(style, {"sum": 0, "n": 0})
        s["sum"] += rating
        s["n"] += 1
    return {k: (v["sum"] / v["n"], v["n"]) for k, v in stats.items()}

OUT_W, OUT_H = 1080, 1920
# Render無料枠(512MB)時代はOOM対策で720x1280に落としていたが、
# Cloud Run(2GB)へ移行したので元の解像度に戻した。文字サイズ等の
# 絶対値はREF_SCALEで比例調整する(今は1.0=無調整)。
REF_SCALE = OUT_W / 1080
MAX_SCENES = 1
MIN_SCENE_SEC = 1.5


def probe_duration(video_path):
    """フレームをデコードせずヘッダ情報だけで動画の長さを取得する(高速)。
    detect_scenes()がタイムアウトした際の1シーンフォールバック用。"""
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    cap.release()
    return (total_frames / fps) if fps else 0.0


def detect_scenes(video_path, min_scene_sec=MIN_SCENE_SEC, max_scenes=MAX_SCENES, sample_fps=4):
    """フレーム差分でカット点を検出し、(開始秒, 終了秒)のリストを返す。
    カットが見つからない場合は動画全体を1シーンとして返す。"""
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    duration = total_frames / fps if fps else 0
    if duration <= 0:
        cap.release()
        return [(0.0, 0.0)]
    if max_scenes <= 1:
        # シーン分割自体が不要な場合、動画全体をフレームデコードして
        # スキャンする(遅い・ハングしうる)処理を丸ごとスキップできる。
        # ヘッダ情報から長さが分かった時点で即座に返す。
        cap.release()
        return [(0.0, duration)]

    step = max(1, int(round(fps / sample_fps)))
    prev_gray = None
    diffs = []
    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % step == 0:
            small = cv2.resize(frame, (64, 114))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            if prev_gray is not None:
                diff = float(np.mean(cv2.absdiff(gray, prev_gray)))
                diffs.append((frame_idx / fps, diff))
            prev_gray = gray
        frame_idx += 1
    cap.release()

    if len(diffs) < 2:
        return [(0.0, duration)]

    scores = np.array([d for _, d in diffs])
    threshold = float(scores.mean() + max(1.5 * scores.std(), 8.0))

    candidates = sorted(((t, d) for t, d in diffs if d > threshold), key=lambda x: -x[1])
    chosen = []
    for t, _ in candidates:
        if all(abs(t - c) >= min_scene_sec for c in chosen):
            chosen.append(t)
        if len(chosen) >= max_scenes - 1:
            break
    chosen.sort()

    bounds = [0.0] + chosen + [duration]
    scenes = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1) if bounds[i + 1] - bounds[i] >= 0.5]
    return scenes or [(0.0, duration)]


def grab_frame_at(video_path, time_sec):
    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_MSEC, max(time_sec, 0) * 1000)
    ret, frame = cap.read()
    if not ret:
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ret, frame = cap.read()
    cap.release()
    return ret, frame


def _edge_density(gray_region):
    if gray_region.size == 0:
        return 0.0
    return float(cv2.Canny(gray_region, 50, 150).mean())


def analyze_zones(frame_bgr):
    """フレームの上部と中段、左上と右上の込み具合(エッジ密度)を比較し、
    テキストとスタンプを置くのに向いているシンプルな領域を選ぶ。"""
    h, w = frame_bgr.shape[:2]
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)

    top_band = gray[int(h * 0.05):int(h * 0.32), :]
    mid_band = gray[int(h * 0.36):int(h * 0.62), :]
    text_zone = "top" if _edge_density(top_band) <= _edge_density(mid_band) else "middle"

    top_left = gray[0:int(h * 0.28), 0:int(w * 0.5)]
    top_right = gray[0:int(h * 0.28), int(w * 0.5):w]
    stamp_corner = "top-right" if _edge_density(top_right) <= _edge_density(top_left) else "top-left"

    return text_zone, stamp_corner


def wrap_text(draw, text, font, max_width):
    lines = []
    current = ""
    for ch in text:
        trial = current + ch
        if draw.textlength(trial, font=font) > max_width and current:
            lines.append(current)
            current = ch
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def contrast_stroke(fill_color):
    """文字色が暗い(黒系)場合は白縁取りに、それ以外は黒縁取りにして
    どんな背景でも視認性を保つ。"""
    return "white" if fill_color.lstrip("#").lower() in ("111111", "000000") else "black"


def draw_stroked_text(draw, pos, text, font, fill="white", stroke_fill="black", stroke_width=None):
    if stroke_width is None:
        stroke_width = max(int(7 * REF_SCALE), 2)
    draw.text(pos, text, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)


def build_text_overlay(title, body, price, zone="top", scale=1.0, font_path=FONT_BOLD, text_color="#FFFFFF"):
    """実際のBuyBee Storiesに合わせ、背景カードなしの太字文字(縁取りあり)を
    写真に直接乗せるスタイル。zoneでキャッチコピー+特徴の開始位置を切り替える
    (画面上部が込んでいるシーンでは中段寄りに下げる)。価格は常に左下。
    scaleは文字サイズの倍率、font_path/text_colorはフォントと文字色
    (いずれもシーンごとに変えられる)。"""
    scale = scale * REF_SCALE
    canvas = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    title_font = ImageFont.truetype(font_path, max(int(76 * scale), 10))
    body_font = ImageFont.truetype(font_path, max(int(54 * scale), 10))
    price_font = ImageFont.truetype(font_path, max(int(64 * scale), 10))
    stroke_color = contrast_stroke(text_color)

    pad_x = int(56 * REF_SCALE)
    max_w = OUT_W - pad_x * 2

    y = int(150 * REF_SCALE) if zone == "top" else int(OUT_H * 0.40)

    if title:
        for line in wrap_text(draw, title, title_font, max_w):
            draw_stroked_text(draw, (pad_x, y), line, title_font, fill=text_color, stroke_fill=stroke_color)
            y += int(92 * scale)

    if body:
        y += int(30 * scale)
        for raw_line in body.split("\n"):
            for line in wrap_text(draw, raw_line, body_font, max_w):
                draw_stroked_text(draw, (pad_x, y), line, body_font, fill=text_color, stroke_fill=stroke_color)
                y += int(70 * scale)

    if price:
        price_y = OUT_H - int(300 * REF_SCALE)
        draw_stroked_text(draw, (pad_x, price_y), price, price_font, fill=text_color, stroke_fill=stroke_color)

    return canvas


def build_stamp(stamp_text, color_name, corner="top-right", font_path=FONT_BOLD):
    """ピンクの「NEW」のような、マーカーで書いたワンポイントスタンプ風の文字画像。
    cornerに応じて傾きの向きを変える(隅に向かって傾く自然な見た目にする)。"""
    color = STAMP_COLORS.get(color_name, STAMP_COLORS["ピンク"])
    font_size = max(int(130 * REF_SCALE), 10)
    font = ImageFont.truetype(font_path, font_size)

    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    text_w = probe.textlength(stamp_text, font=font)
    pad = int(100 * REF_SCALE)
    canvas_w, canvas_h = int(text_w) + pad, int(font_size * 1.5)

    badge = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(badge)
    draw_stroked_text(
        draw, (int(50 * REF_SCALE), int(20 * REF_SCALE)), stamp_text, font,
        fill=color, stroke_fill="white", stroke_width=max(int(8 * REF_SCALE), 2),
    )

    angle = -10 if corner == "top-right" else 10
    return badge.rotate(angle, expand=True, resample=Image.BICUBIC)


def stamp_target_position(stamp_img, corner="top-right", text_zone="middle", margin_x=None):
    """text_zoneが"top"(キャッチコピーが上部に来る)場合は、スタンプを
    もう少し下げてタイトルとの重なりを避ける。"""
    if margin_x is None:
        margin_x = int(50 * REF_SCALE)
    w, _ = stamp_img.size
    margin_y = int(320 * REF_SCALE) if text_zone == "top" else int(90 * REF_SCALE)
    if corner == "top-right":
        return OUT_W - w - margin_x, margin_y
    return margin_x, margin_y


FADE_SEC = 0.3


def _parse_hms(s):
    try:
        h, m, sec = s.split(":")
        return int(h) * 3600 + int(m) * 60 + float(sec)
    except (ValueError, AttributeError):
        return None


def burn_video_scenes(video_path, scenes, out_path, progress_cb=None):
    """scenes: [{start, end, text_img(PIL), stamp_img(PIL), stamp_xy:(x,y)}, ...]
    各シーンのテキスト・スタンプを、境界でふわっとクロスフェードしながら
    その時間帯だけ表示するよう連結する。progress_cb(0.0〜1.0)が渡されれば、
    ffmpegの進捗(-progress)を読み取ってその都度呼び出す(進捗%表示用)。"""
    tmp_files = []
    inputs = ["-threads", "2", "-i", video_path]
    filters = [
        f"[0:v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H}[v0]"
    ]

    prev_label = "v0"
    input_index = 1
    for i, scene in enumerate(scenes):
        text_path = tempfile.NamedTemporaryFile(delete=False, suffix=".png").name
        stamp_path = tempfile.NamedTemporaryFile(delete=False, suffix=".png").name
        scene["text_img"].save(text_path)
        scene["stamp_img"].save(stamp_path)
        tmp_files += [text_path, stamp_path]

        # -loop 1: 静止画を無限ストリームとして扱う。付けないとenableが遅れて
        # 有効になるシーンで、画像入力が先にEOFして描画されないバグがある。
        inputs += ["-loop", "1", "-i", text_path, "-loop", "1", "-i", stamp_path]
        text_in, stamp_in = input_index, input_index + 1
        input_index += 2

        start, end = scene["start"], scene["end"]
        sx, sy = scene["stamp_xy"]
        d = max(min(FADE_SEC, (end - start) / 2), 0.05)
        fade_in_st = max(start, 0)
        fade_out_st = max(end - d, fade_in_st)

        # fadeフィルタでalphaチャンネルだけをふわっと上げ下げしてから重ねる
        # ことで、シーンの切り替わりが瞬間的なカットではなく自然な
        # クロスフェードになる(enableでの瞬時on/offはやめた)。
        text_faded, stamp_faded = f"tf{i}", f"sf{i}"
        filters.append(
            f"[{text_in}:v]format=rgba,"
            f"fade=t=in:st={fade_in_st:.2f}:d={d:.2f}:alpha=1,"
            f"fade=t=out:st={fade_out_st:.2f}:d={d:.2f}:alpha=1[{text_faded}]"
        )
        filters.append(
            f"[{stamp_in}:v]format=rgba,"
            f"fade=t=in:st={fade_in_st:.2f}:d={d:.2f}:alpha=1,"
            f"fade=t=out:st={fade_out_st:.2f}:d={d:.2f}:alpha=1[{stamp_faded}]"
        )

        mid_label = f"vt{i}"
        out_label = f"v{i + 1}"
        # shortest=1: overlay側のeof_action既定(repeat)だと、無限loopの画像入力に
        # 引っ張られて本編動画が終わった後も出力が終わらなくなるため、本編側基準で終了させる。
        filters.append(f"[{prev_label}][{text_faded}]overlay=0:0:shortest=1[{mid_label}]")
        filters.append(f"[{mid_label}][{stamp_faded}]overlay={sx}:{sy}:shortest=1[{out_label}]")
        prev_label = out_label

    filter_complex = ";".join(filters)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", f"[{prev_label}]",
        # 0:a? だと全ての音声トラックを含めようとし、新型iPhoneの空間オーディオ
        # (apacコーデック)のような未対応トラックが混ざっていると全体が失敗する。
        # 最初の音声トラックだけを使う。
        "-map", "0:a:0?",
        # Cloud Run(2GB/2CPU)移行に伴い、Render無料枠向けの省メモリ設定
        # (threads=1, crf=23)から画質・速度優先の設定に戻した。
        "-threads", "2",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac",
        "-shortest",
        "-progress", "pipe:1", "-nostats",
        out_path,
    ]
    # 全シーンがskipされ scenes が空になった場合(分析が全滅した場合など)、
    # シーンの終端時刻から総時間を推定できないため、元動画自体の長さを使う。
    total_duration = max((s["end"] for s in scenes), default=0.0) or probe_duration(video_path) or 1.0
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # stdout(-progressの進捗行)とstderr(通常のログ)を同時にpipeで
        # 溜めると、片方を読んでいる間にもう片方のバッファが溢れて
        # ffmpeg自体が書き込みブロックし、デッドロック(ハング)する。
        # stderrは別スレッドで並行して読み切っておく。
        stderr_chunks = []
        stderr_thread = threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True)
        stderr_thread.start()
        try:
            for line in proc.stdout:
                line = line.strip()
                if progress_cb and line.startswith("out_time="):
                    sec = _parse_hms(line.split("=", 1)[1])
                    if sec is not None:
                        try:
                            progress_cb(min(sec / total_duration, 0.99))
                        except Exception:
                            pass
            returncode = proc.wait(timeout=300)
            stderr_thread.join(timeout=10)
            stderr_data = "".join(stderr_chunks)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
    finally:
        for p in tmp_files:
            os.unlink(p)
    if returncode != 0:
        raise RuntimeError(stderr_data[-3000:])
    return out_path


def fetch_product_page_text(url, max_chars=2000, timeout=10):
    """商品ページのURLからタイトル・説明・本文テキストを抜き出す。
    取得や解析に失敗した場合は例外を投げる(呼び出し側で警告表示して
    URLなしの分析にフォールバックする想定)。"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BuyBeeStoriesBot/1.0"}
    res = requests.get(url, headers=headers, timeout=timeout)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    parts = []
    if soup.title and soup.title.string:
        parts.append(soup.title.string.strip())
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc and meta_desc.get("content"):
        parts.append(meta_desc["content"].strip())

    body_text = soup.get_text(separator="\n")
    body_text = re.sub(r"\n{2,}", "\n", body_text).strip()
    parts.append(body_text)

    combined = "\n".join(parts)
    return combined[:max_chars]
