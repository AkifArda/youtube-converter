import os
import uuid
import threading
import time
import requests
from flask import Flask, request, jsonify, send_file, render_template, after_this_request

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_FOLDER = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

sessions = {}

COBALT_API = "https://api.cobalt.tools"


def cleanup_old_files(max_age_seconds=1800):
    try:
        for entry in os.scandir(DOWNLOAD_FOLDER):
            if entry.is_dir():
                for f in os.scandir(entry.path):
                    if time.time() - f.stat().st_mtime > max_age_seconds:
                        os.remove(f.path)

                try:
                    if not os.listdir(entry.path):
                        os.rmdir(entry.path)
                except OSError:
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
    session["status"] = "downloading"
    session["progress"] = 10

    try:
        if fmt == "mp3":
            payload = {
                "url": url,
                "downloadMode": "audio",
                "audioFormat": "mp3",
                "audioBitrate": quality,
            }
        else:
            payload = {
                "url": url,
                "downloadMode": "auto",
                "videoQuality": quality,
            }

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        session["progress"] = 20

        response = requests.post(
            COBALT_API,
            json=payload,
            headers=headers,
            timeout=30,
        )

        response.raise_for_status()

        data = response.json()
        session["progress"] = 40

        if data.get("status") == "error":
            raise Exception(
                data.get("error", {}).get("code", "Cobalt API hatası")
            )

        download_url = data.get("url")

        if not download_url:
            raise Exception("Cobalt indirme bağlantısı alınamadı.")

        session["progress"] = 50

        session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
        os.makedirs(session_dir, exist_ok=True)

        filename = data.get("filename") or f"download.{fmt}"

        if not filename.endswith(f".{fmt}"):
            filename = f"{os.path.splitext(filename)[0]}.{fmt}"

        filepath = os.path.join(session_dir, filename)

        with requests.get(download_url, stream=True, timeout=120) as r:
            r.raise_for_status()

            total = int(r.headers.get("content-length", 0))
            downloaded = 0

            with open(filepath, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)

                        if total:
                            pct = 50 + int(downloaded / total * 45)
                            session["progress"] = min(pct, 95)

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
        download_name=session["filename"],
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
