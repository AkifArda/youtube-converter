import os
import uuid
import threading
import time
import requests
from flask import Flask, request, jsonify, send_file, render_template, after_this_request
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Klasör yapılandırması
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_FOLDER = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# İş parçacığı (thread) güvenliği için kilit ve oturum havuzu
sessions_lock = threading.Lock()
sessions = {}

COBALT_API = "https://api.cobalt.tools"

def cleanup_old_files(max_age_seconds=1800):
    """Belirli bir süreden eski kalmış indirme klasörlerini temizler."""
    try:
        for entry in os.scandir(DOWNLOAD_FOLDER):
            if entry.is_dir():
                dir_empty = True
                for f in os.scandir(entry.path):
                    if time.time() - f.stat().st_mtime > max_age_seconds:
                        try:
                            os.remove(f.path)
                        except Exception:
                            dir_empty = False
                    else:
                        dir_empty = False
                if dir_empty:
                    try:
                        os.rmdir(entry.path)
                    except Exception:
                        pass
    except Exception:
        pass

def periodic_cleanup():
    """Arka planda çalışan temizlik döngüsü."""
    while True:
        time.sleep(900)  # 15 dakikada bir çalışır
        cleanup_old_files()

# Arka plan temizlik iş parçacığını başlat
threading.Thread(target=periodic_cleanup, daemon=True).start()

def run_download(session_id, url, fmt, quality):
    with sessions_lock:
        if session_id in sessions:
            sessions[session_id]["status"] = "downloading"
            sessions[session_id]["progress"] = 10

    try:
        # Cobalt API Güncel Payload Kurulumu
        if fmt == "mp3":
            payload = {
                "url": url,
                "audioFormat": "mp3",
                "audioBitrate": quality,  # Örn: "320", "192"
                "filenamePattern": "basic"
            }
        else:
            payload = {
                "url": url,
                "videoQuality": quality if quality in ["1080", "720", "480", "360"] else "720",
                "filenamePattern": "basic"
            }

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

        with sessions_lock:
            if session_id in sessions: sessions[session_id]["progress"] = 25

        response = requests.post(
            f"{COBALT_API}/",
            json=payload,
            headers=headers,
            timeout=30,
        )

        if response.status_code != 200:
            raise Exception(f"Cobalt API hatası (Kod: {response.status_code})")

        data = response.json()
        
        with sessions_lock:
            if session_id in sessions: sessions[session_id]["progress"] = 45

        # Durum kontrolü
        status = data.get("status")
        if status == "error":
            error_msg = data.get("error", {}).get("code", "Bilinmeyen Cobalt hatası")
            raise Exception(f"Cobalt Hatası: {error_msg}")

        download_url = data.get("url")
        if not download_url:
            raise Exception("Cobalt'tan indirme bağlantısı alınamadı.")

        with sessions_lock:
            if session_id in sessions: sessions[session_id]["progress"] = 55

        # Dosya ismi güvenliği ve klasörleme
        session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
        os.makedirs(session_dir, exist_ok=True)

        raw_filename = data.get("filename") or f"download_{session_id[:8]}.{fmt}"
        # Dosya uzantısını garantiye al ve işletim sistemi için güvenli yap
        clean_filename = secure_filename(raw_filename)
        if not clean_filename.endswith(f".{fmt}"):
            clean_filename = os.path.splitext(clean_filename)[0] + f".{fmt}"

        filepath = os.path.join(session_dir, clean_filename)

        # Stream olarak sunucuya indirme aşaması
        with requests.get(download_url, stream=True, timeout=120) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            downloaded = 0
            
            with open(filepath, "wb") as f:
                for chunk in r.iter_content(chunk_size=16384): # Buffer boyutu optimize edildi
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = 55 + int(downloaded / total * 40)
                            with sessions_lock:
                                if session_id in sessions:
                                    sessions[session_id]["progress"] = min(pct, 95)

        # Tamamlanma durumu güncellemesi
        with sessions_lock:
            if session_id in sessions:
                sessions[session_id]["filepath"] = filepath
                sessions[session_id]["filename"] = clean_filename
                sessions[session_id]["progress"] = 100
                sessions[session_id]["status"] = "done"

    except Exception as e:
        with sessions_lock:
            if session_id in sessions:
                sessions[session_id]["status"] = "error"
                sessions[session_id]["error"] = str(e)
                sessions[session_id]["progress"] = 0


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/start", methods=["POST"])
def start_download():
    # İstek geldiğinde eski dosyaları asenkron olmasa da hızlıca bir tara
    cleanup_old_files()
    
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    fmt = data.get("format", "mp3")
    quality = data.get("quality", "192")

    if not url:
        return jsonify({"error": "Lütfen geçerli bir URL girin."}), 400

    session_id = str(uuid.uuid4())
    
    with sessions_lock:
        sessions[session_id] = {
            "progress": 0,
            "status": "starting",
            "filename": None,
            "filepath": None,
            "error": None,
        }

    # İndirme işlemini arka planda tetikle
    threading.Thread(
        target=run_download,
        args=(session_id, url, fmt, quality),
        daemon=True,
    ).start()

    return jsonify({"session_id": session_id})


@app.route("/api/progress/<session_id>")
def get_progress(session_id):
    with sessions_lock:
        session = sessions.get(session_id)
    
    if not session:
        return jsonify({"error": "Oturum bulunamadı veya süresi doldu."}), 404
        
    return jsonify({
        "progress": session["progress"],
        "status": session["status"],
        "filename": session["filename"],
        "error": session["error"],
    })


@app.route("/api/download/<session_id>")
def download_file(session_id):
    with sessions_lock:
        session = sessions.get(session_id)

    if not session or session["status"] != "done":
        return jsonify({"error": "Dosya henüz hazır değil veya session silindi."}), 404

    filepath = session["filepath"]
    filename = session["filename"]

    if not os.path.exists(filepath):
        return jsonify({"error": "Dosya sunucuda bulunamadı."}), 404

    @after_this_request
    def remove_file(response):
        try:
            # Dosyayı gönderdikten hemen sonra yerel diskten kaldır
            if os.path.exists(filepath):
                os.remove(filepath)
            session_dir = os.path.dirname(filepath)
            if os.path.isdir(session_dir) and not os.listdir(session_dir):
                os.rmdir(session_dir)
            
            # RAM'deki session kaydını temizle
            with sessions_lock:
                sessions.pop(session_id, None)
        except Exception:
            pass
        return response

    return send_file(filepath, as_attachment=True, download_name=filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # Local testler için debug=False stabil kalmasını sağlar
    app.run(host="0.0.0.0", port=port, debug=False)
