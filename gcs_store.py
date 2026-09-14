"""完成した動画・プッシュ通知の購読情報をGoogle Cloud Storageに永続化する。

Cloud Runのインスタンスはアイドル時に自動でスケールダウン/再起動され、
その際コンテナのローカルディスクとメモリ上の状態は全て消える
(JOBS辞書やPUSH_SUBSCRIPTIONSがまさにこれ)。「完成した動画が見れない」
「完了通知が来ない」という不具合の根本原因はこれだったため、動画本体と
購読情報だけはインスタンスの外側(GCS)に置いて生き残らせる。
"""
import json
import os

from google.cloud import storage

BUCKET_NAME = os.environ.get("VIDEO_BUCKET", "buybee-stories-videos")
MAX_LIBRARY_BYTES = 5 * 1024 ** 3  # 5GB。超えたら古い動画から自動で消す。
PUSH_SUBS_BLOB = "_push_subscriptions.json"

_client = None


def _bucket():
    global _client
    if _client is None:
        _client = storage.Client()
    return _client.bucket(BUCKET_NAME)


def _video_blobs():
    return [b for b in _bucket().list_blobs() if not b.name.startswith("_")]


def upload_video(local_path, object_name, content_type="video/mp4"):
    blob = _bucket().blob(object_name)
    blob.upload_from_filename(local_path, content_type=content_type)
    enforce_capacity_limit()
    return object_name


def enforce_capacity_limit():
    """容量上限を超えていたら、古い(作成日時が古い)動画から順に削除する。"""
    blobs = sorted(_video_blobs(), key=lambda b: b.time_created)
    total = sum(b.size or 0 for b in blobs)
    i = 0
    while total > MAX_LIBRARY_BYTES and i < len(blobs):
        b = blobs[i]
        total -= (b.size or 0)
        try:
            b.delete()
        except Exception:
            pass
        i += 1


def list_videos():
    """新しい順のリスト。[{name, size, created}, ...]"""
    blobs = sorted(_video_blobs(), key=lambda b: b.time_created, reverse=True)
    return [
        {"name": b.name, "size": b.size, "created": b.time_created.isoformat() if b.time_created else None}
        for b in blobs
    ]


def get_video_blob(object_name):
    if object_name.startswith("_"):
        return None
    return _bucket().blob(object_name)


def load_push_subscriptions():
    # サーバー起動時に呼ばれるため、GCSに一時的に繋がらない程度のことで
    # 起動自体がクラッシュしないよう、丸ごとtry/exceptで守る。
    try:
        blob = _bucket().blob(PUSH_SUBS_BLOB)
        if not blob.exists():
            return []
        return json.loads(blob.download_as_text())
    except Exception:
        return []


def save_push_subscriptions(subs):
    blob = _bucket().blob(PUSH_SUBS_BLOB)
    blob.upload_from_string(json.dumps(subs), content_type="application/json")


FEEDBACK_BLOB = "_feedback_log.json"


def load_feedback():
    # core.log_feedbackはコンテナのローカルディスクに書いていたため、
    # インスタンス再起動のたびに★評価が消えていた(動画・通知購読と同じ
    # 根本原因)。評価もGCSに永続化する。
    try:
        blob = _bucket().blob(FEEDBACK_BLOB)
        if not blob.exists():
            return []
        return json.loads(blob.download_as_text())
    except Exception:
        return []


def append_feedback(record):
    records = load_feedback()
    records.append(record)
    blob = _bucket().blob(FEEDBACK_BLOB)
    blob.upload_from_string(json.dumps(records, ensure_ascii=False), content_type="application/json")
