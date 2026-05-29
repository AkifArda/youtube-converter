import os
import uuid
import threading
import time
import glob
from flask import Flask, request, jsonify, send_file, render_template, after_this_request
import yt_dlp

app = Flask(__name__)

# Geçici indirme klasörü
DOWNLOAD_FOLDER = "/tmp/yt_downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# Aktif indirme oturumlarını takip eden sözlük
# { session_id: { "progress": 0-100, "status": "...", "filename": "...", "filepath": "..." } }
sessions = {}


def cleanup_old_files(max_age_seconds=1800):
    """30 dakikadan eski dosyaları temizler."""
    now = time.time()
    pattern = os.path.join(DOWNLOAD_FOLDER, "*")
    for filepath in glob.glob(pattern):
        try:
            if os.path.isfile(filepath):
                age = now - os.path.getmtime(filepath)
                if age > max_age_seconds:
                    os.remove(filepath)
        except Exception:
            pass


def periodic_cleanup():
    """Arka planda her 15 dakikada bir temizlik yapar."""
    while True:
        time.sleep(900)
        cleanup_old_files()


# Arka plan temizlik thread'ini başlat
cleanup_thread = threading.Thread(target=periodic_cleanup, daemon=True)
cleanup_thread.start()


def run_download(session_id, url, fmt, quality):
    """yt-dlp ile indirme işlemini gerçekleştirir."""
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

    # Format seçenekleri
    if fmt == "mp3":
        ydl_opts = {
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
            "quiet": True,
            "no_warnings": True,
        }
    else:  # mp4
        quality_map = {
            "1080": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
            "720":  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
            "480":  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best",
            "360":  "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360][ext=mp4]/best",
        }
        ydl_opts = {
            "format": quality_map.get(quality, quality_map["720"]),
            "outtmpl": output_template,
            "progress_hooks": [progress_hook],
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # İndirilen dosyayı bul
        session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
        files = os.listdir(session_dir)
        if not files:
            raise FileNotFoundError("İndirilen dosya bulunamadı.")

        # En yeni dosyayı al
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
    """İndirme işlemini başlatır, session_id döner."""
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

    thread = threading.Thread(
        target=run_download,
        args=(session_id, url, fmt, quality),
        daemon=True,
    )
    thread.start()

    return jsonify({"session_id": session_id})


@app.route("/api/progress/<session_id>")
def get_progress(session_id):
    """İndirme ilerleme durumunu döner."""
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
    """Tamamlanan dosyayı kullanıcıya gönderir, ardından siler."""
    session = sessions.get(session_id)
    if not session or session["status"] != "done":
        return jsonify({"error": "Dosya hazır değil."}), 404

    filepath = session["filepath"]
    filename = session["filename"]

    if not os.path.exists(filepath):
        return jsonify({"error": "Dosya sunucuda bulunamadı."}), 404

    # Gönderildikten sonra dosya + klasörü temizle
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

    return send_file(
        filepath,
        as_attachment=True,
        download_name=filename,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
