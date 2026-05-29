import os
import uuid
import threading
import time
import glob
import imageio_ffmpeg
from flask import Flask, request, jsonify, send_file, render_template, after_this_request
import yt_dlp

os.environ["FFMPEG_BINARY"] = imageio_ffmpeg.get_ffmpeg_exe()

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_FOLDER = os.path.join(BASE_DIR, "downloads")
COOKIES_FILE = os.path.join(BASE_DIR, "cookies.txt")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

sessions = {}


def cleanup_old_files(max_age_seconds=1800):
    now = time.time()
    for entry in os.scandir(DOWNLOAD_FOLDER):
        try:
            if entry.is_dir():
                for f in os.scandir(entry.path):
                    if time.time() - f.stat().st_mtime > max_age_seconds:
                        os.remove(f.path)
                try:
                    os.rmdir(entry.path)
                except Exception:
                    pass
        except Exception:
            pass


def periodic_cleanup():
    while True:
        time.sleep(900)
        cleanup_old_files()


threading.Thread(target=periodic_cleanup, daemon=True).start()


def get_common_opts():
    opts = {
        "quiet": False,
        "no_warnings": False,
        "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
        "extractor_args": {
            "youtube": {
                "player_client": ["tv_embedded", "web"],
            }
        },
    }
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


def run_download(session_id, url, fmt, quality):
    session = sessions[session_id]
    session["status"] = "downloading"

    output_template = os.path.join(DOWNLOAD_FOLDER, session_id, "%(title)s.%(ext)s")
    os.makedirs(os.path.join(DOWNLOAD_FOLDER, session_id), exist_ok=True)

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            if total:
                pct = int(downloaded / total * 90)
                session["progress"] = max(session["progress"], pct)
        elif d["status"] == "finished":
            session["progress"] = 95
            session["status"] = "processing"

    common = get_common_opts()

    if fmt == "mp3":
        ydl_opts = {
            **common,
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "progress_hooks": [progress_hook],
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": quality,
                }
            ],
        }
    else:
        quality_map = {
            "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
            "720":  "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
            "480":  "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
            "360":  "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
        }
        ydl_opts = {
            **common,
            "format": quality_map.get(quality, quality_map["720"]),
            "outtmpl": output_template,
            "progress_hooks": [progress_hook],
            "merge_output_format": "mp4",
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
        files = os.listdir(session_dir)
        if not files:
            raise FileNotFoundError("İndirilen dosya bulunamadı.")

        filepath = max(
            [os.path.join(session_dir, f) for f in files],
            key=os.path.getmtime
        )
        filename = os.path.basename(filepath)

        session["filepath"] = filepath
        session["filename"] = filename
        session["progress"] = 100
        session["status"] = "done"

    except Exception as e:
        session["status"] = "error"
        session["error"] = str(e)
        session["progress"] = 0


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/start", methods=["POST"])
def start_download():
    cleanup_old_files()
    data = request.get_json()
    url = data.get("url", "").strip()
    fmt = data.get("format", "mp3")
    quality = data.get("quality", "192")

    if not url:
        return jsonify({"error": "URL boş olamaz."}), 400

    session_id = str(uuid.uuid4())
    sessions[session_id] = {
        "progress": 0,
        "status": "starting",
        "filename": None,
        "filepath": None,
        "error": None,
    }

    threading.Thread(
        target=run_download,
        args=(session_id, url, fmt, quality),
        daemon=True,
    ).start()

    return jsonify({"session_id": session_id})


@app.route("/api/progress/<session_id>")
def get_progress(session_id):
    session = sessions.get(session_id)
    if not session:
        return jsonify({"error": "Oturum bulunamadı."}), 404
    return jsonify({
        "progress": session["progress"],
        "status": session["status"],
        "filename": session["filename"],
        "error": session["error"],
    })


@app.route("/api/download/<session_id>")
def download_file(session_id):
    session = sessions.get(session_id)
    if not session or session["status"] != "done":
        return jsonify({"error": "Dosya hazır değil."}), 404

    filepath = session["filepath"]
    filename = session["filename"]

    if not os.path.exists(filepath):
        return jsonify({"error": "Dosya sunucuda bulunamadı."}), 404

    @after_this_request
    def remove_file(response):
        try:
            os.remove(filepath)
            session_dir = os.path.dirname(filepath)
            if os.path.isdir(session_dir) and not os.listdir(session_dir):
                os.rmdir(session_dir)
            sessions.pop(session_id, None)
        except Exception:
            pass
        return response

    return send_file(filepath, as_attachment=True, download_name=filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
