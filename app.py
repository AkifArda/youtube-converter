import os
import uuid
import threading
import time
import yt_dlp

from flask import (
    Flask,
    request,
    jsonify,
    send_file,
    render_template,
    after_this_request,
)

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_FOLDER = os.path.join(BASE_DIR, "downloads")

os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

sessions = {}


def cleanup_old_files(max_age_seconds=1800):
    try:
        for folder in os.scandir(DOWNLOAD_FOLDER):
            if folder.is_dir():
                for file in os.scandir(folder.path):
                    if time.time() - file.stat().st_mtime > max_age_seconds:
                        os.remove(file.path)

                try:
                    if not os.listdir(folder.path):
                        os.rmdir(folder.path)
                except Exception:
                    pass
    except Exception:
        pass


def periodic_cleanup():
    while True:
        time.sleep(900)
        cleanup_old_files()


threading.Thread(target=periodic_cleanup, daemon=True).start()


def run_download(session_id, url, fmt, quality):
    session = sessions[session_id]

    try:
        session["status"] = "downloading"
        session["progress"] = 10

        session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
        os.makedirs(session_dir, exist_ok=True)

        if fmt == "mp3":
            output_template = os.path.join(
                session_dir,
                "%(title)s.%(ext)s"
            )

            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": output_template,
                "noplaylist": True,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": quality,
                    }
                ],
            }

        else:
            output_template = os.path.join(
                session_dir,
                "%(title)s.%(ext)s"
            )

            ydl_opts = {
                "format": f"bestvideo[height<={quality}]+bestaudio/best/best",
                "merge_output_format": "mp4",
                "outtmpl": output_template,
                "noplaylist": True,
            }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        files = os.listdir(session_dir)

        if not files:
            raise Exception("Dosya indirilemedi.")

        filename = files[0]
        filepath = os.path.join(session_dir, filename)

        session["filename"] = filename
        session["filepath"] = filepath
        session["progress"] = 100
        session["status"] = "done"

    except Exception as e:
        session["status"] = "error"
        session["error"] = str(e)
        print("yt-dlp error:", e)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/start", methods=["POST"])
def start_download():
    cleanup_old_files()

    data = request.get_json(force=True)

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

    return jsonify(session)


@app.route("/api/download/<session_id>")
def download_file(session_id):
    session = sessions.get(session_id)

    if not session or session["status"] != "done":
        return jsonify({"error": "Dosya hazır değil."}), 404

    filepath = session["filepath"]

    if not filepath or not os.path.exists(filepath):
        return jsonify({"error": "Dosya bulunamadı."}), 404

    @after_this_request
    def cleanup(response):
        try:
            os.remove(filepath)

            folder = os.path.dirname(filepath)

            if os.path.isdir(folder) and not os.listdir(folder):
                os.rmdir(folder)

            sessions.pop(session_id, None)

        except Exception:
            pass

        return response

    return send_file(
        filepath,
        as_attachment=True,
        download_name=session["filename"]
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
