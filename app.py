"""
app.py - Flask web app for the silence-removal + speed-matching tool.

Upload an "upper" and a "lower" video in the browser; the server removes
silence from the lower video, speeds up the upper video to match its new
duration, and gives you both files back as downloads.
"""

import os
import shutil
import time
import uuid
from pathlib import Path

from flask import (Flask, render_template, request, send_from_directory,
                    redirect, url_for, flash)
from werkzeug.utils import secure_filename

from video_processing import process_videos

BASE_DIR = Path(__file__).parent
TMP_DIR = BASE_DIR / "tmp"
TMP_DIR.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {"mp4", "mov", "mkv", "avi", "webm", "m4v", "flv"}
MAX_AGE_SECONDS = 60 * 60  # clean up job folders older than 1 hour

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
# Keep this modest — Railway's free/trial tier has limited RAM/CPU and
# large uploads will eat through it fast. Adjust if you upgrade plans.
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024  # 300 MB total request


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def cleanup_old_jobs():
    now = time.time()
    for job_dir in TMP_DIR.iterdir():
        if job_dir.is_dir() and (now - job_dir.stat().st_mtime) > MAX_AGE_SECONDS:
            shutil.rmtree(job_dir, ignore_errors=True)


@app.route("/", methods=["GET"])
def index():
    cleanup_old_jobs()
    return render_template("index.html")


@app.route("/process", methods=["POST"])
def process():
    cleanup_old_jobs()

    upper_file = request.files.get("upper")
    lower_file = request.files.get("lower")

    if not upper_file or not lower_file or upper_file.filename == "" or lower_file.filename == "":
        flash("Please choose both an upper and a lower video.")
        return redirect(url_for("index"))

    if not (allowed_file(upper_file.filename) and allowed_file(lower_file.filename)):
        flash("Unsupported file type. Allowed: " + ", ".join(sorted(ALLOWED_EXTENSIONS)))
        return redirect(url_for("index"))

    try:
        noise_db = float(request.form.get("noise_db", -30))
        min_silence = float(request.form.get("min_silence", 0.5))
        padding = float(request.form.get("padding", 0.08))
        use_crf = request.form.get("use_crf") == "on"
        crf = int(request.form.get("crf", 18)) if use_crf else None
    except ValueError:
        flash("Invalid numeric setting.")
        return redirect(url_for("index"))

    job_id = uuid.uuid4().hex
    job_dir = TMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    upper_path = job_dir / secure_filename(upper_file.filename)
    lower_path = job_dir / secure_filename(lower_file.filename)
    upper_file.save(upper_path)
    lower_file.save(lower_path)

    log_lines = []

    def log(msg):
        log_lines.append(msg)

    try:
        result = process_videos(
            upper_path, lower_path, job_dir,
            noise_db=noise_db, min_silence=min_silence,
            padding=padding, crf=crf, log=log,
        )
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(job_dir, ignore_errors=True)
        flash(f"Processing failed: {exc}")
        return redirect(url_for("index"))

    return render_template(
        "result.html",
        job_id=job_id,
        lower_name=Path(result["lower_out"]).name,
        upper_name=Path(result["upper_out"]).name,
        result=result,
        log_lines=log_lines,
    )


@app.route("/download/<job_id>/<filename>")
def download(job_id, filename):
    job_dir = TMP_DIR / secure_filename(job_id)
    safe_name = secure_filename(filename)
    if not (job_dir / safe_name).exists():
        flash("File no longer available (temporary files are cleaned up after an hour).")
        return redirect(url_for("index"))
    return send_from_directory(job_dir, safe_name, as_attachment=True)


@app.errorhandler(413)
def too_large(_e):
    flash("Upload too large for this server's limit.")
    return redirect(url_for("index"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
