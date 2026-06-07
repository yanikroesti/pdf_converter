import os
import io
import uuid
import base64
import zipfile
import tempfile
import shutil
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from pdf2image import convert_from_path, pdfinfo_from_path
from PIL import Image

app = Flask(__name__)
CORS(app)

UPLOAD_DIR = tempfile.mkdtemp()
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


def cleanup_session(session_id: str):
    session_path = os.path.join(UPLOAD_DIR, session_id)
    if os.path.exists(session_path):
        shutil.rmtree(session_path)


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/api/upload", methods=["POST"])
def upload_pdf():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "File must be a PDF"}), 400

    data = file.read()
    if len(data) > MAX_FILE_SIZE:
        return jsonify({"error": "File too large (max 50 MB)"}), 400

    session_id = str(uuid.uuid4())
    session_dir = os.path.join(UPLOAD_DIR, session_id)
    os.makedirs(session_dir)

    pdf_path = os.path.join(session_dir, "input.pdf")
    with open(pdf_path, "wb") as f:
        f.write(data)

    try:
        info = pdfinfo_from_path(pdf_path)
        page_count = info["Pages"]
    except Exception as e:
        cleanup_session(session_id)
        return jsonify({"error": f"Failed to read PDF: {str(e)}"}), 400

    # Generate low-res thumbnails for preview (72 DPI)
    try:
        thumbs = convert_from_path(pdf_path, dpi=72, fmt="jpeg")
    except Exception as e:
        cleanup_session(session_id)
        return jsonify({"error": f"Failed to generate previews: {str(e)}"}), 400

    thumbnails = []
    for img in thumbs:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        thumbnails.append(f"data:image/jpeg;base64,{b64}")

    return jsonify({
        "session_id": session_id,
        "page_count": page_count,
        "filename": file.filename,
        "thumbnails": thumbnails
    })


@app.route("/api/convert", methods=["POST"])
def convert():
    body = request.get_json()
    session_id = body.get("session_id")
    pages = body.get("pages", [])           # 1-indexed list; empty = all
    fmt = body.get("format", "PNG").upper()  # PNG or JPG
    dpi = int(body.get("dpi", 150))
    quality = int(body.get("quality", 90))  # JPEG quality

    if fmt not in ("PNG", "JPG", "JPEG"):
        return jsonify({"error": "Invalid format"}), 400
    if not (72 <= dpi <= 600):
        return jsonify({"error": "DPI must be between 72 and 600"}), 400

    pdf_path = os.path.join(UPLOAD_DIR, session_id, "input.pdf")
    if not os.path.exists(pdf_path):
        return jsonify({"error": "Session not found or expired"}), 404

    try:
        info = pdfinfo_from_path(pdf_path)
        total_pages = info["Pages"]

        if not pages:
            pages = list(range(1, total_pages + 1))
        else:
            pages = [p for p in pages if 1 <= p <= total_pages]

        pil_fmt = "JPEG" if fmt in ("JPG", "JPEG") else "PNG"
        ext = "jpg" if pil_fmt == "JPEG" else "png"

        images = convert_from_path(
            pdf_path,
            dpi=dpi,
            fmt=pil_fmt.lower(),
            first_page=min(pages),
            last_page=max(pages)
        )

        # Map back to the requested page subset
        page_offset = min(pages) - 1
        selected_images = []
        for p in pages:
            idx = p - min(pages)
            if idx < len(images):
                selected_images.append((p, images[idx]))

        if len(selected_images) == 1:
            # Single page — return image directly
            page_num, img = selected_images[0]
            buf = io.BytesIO()
            if pil_fmt == "JPEG":
                img.save(buf, format="JPEG", quality=quality)
            else:
                img.save(buf, format="PNG", optimize=True)
            buf.seek(0)
            mime = "image/jpeg" if pil_fmt == "JPEG" else "image/png"
            filename = f"page_{page_num}.{ext}"
            return send_file(buf, mimetype=mime, as_attachment=True, download_name=filename)

        # Multiple pages — return ZIP
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for page_num, img in selected_images:
                img_buf = io.BytesIO()
                if pil_fmt == "JPEG":
                    img.save(img_buf, format="JPEG", quality=quality)
                else:
                    img.save(img_buf, format="PNG", optimize=True)
                zf.writestr(f"page_{page_num:03d}.{ext}", img_buf.getvalue())

        zip_buf.seek(0)
        return send_file(zip_buf, mimetype="application/zip",
                         as_attachment=True, download_name="converted_pages.zip")

    except Exception as e:
        return jsonify({"error": f"Conversion failed: {str(e)}"}), 500


@app.route("/api/cleanup", methods=["POST"])
def cleanup():
    body = request.get_json()
    session_id = body.get("session_id")
    if session_id:
        cleanup_session(session_id)
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)