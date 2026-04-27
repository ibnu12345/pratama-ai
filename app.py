from flask import Flask, render_template, request, jsonify, session, send_file, Response, stream_with_context
from groq import Groq
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from fpdf import FPDF
import os, logging, tempfile, json
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "pratama-secret-key")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///pratama.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_KEY = os.environ.get("GROQ_API_KEY")
if not API_KEY:
    raise RuntimeError("GROQ_API_KEY tidak ditemukan!")
client = Groq(api_key=API_KEY)

class Percakapan(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    sesi_id    = db.Column(db.String(100))
    pertanyaan = db.Column(db.Text)
    jawaban    = db.Column(db.Text)
    rating     = db.Column(db.Integer, nullable=True)
    waktu      = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    db.create_all()

SISTEM = """Kamu adalah PratamaAI, asisten pembelajaran Pendidikan Agama Islam (PAI) yang dikembangkan oleh Muhammad Ibnu Setiawan Pratama.

IDENTITAS DIRI:
- Nama: PratamaAI
- Pengembang: Muhammad Ibnu Setiawan Pratama
- Perkenalkan diri HANYA jika ditanya siapa kamu atau saat pertama kali disapa. Jangan perkenalkan diri berulang di setiap jawaban.

ATURAN MENJAWAB:
1. Jawab HANYA pertanyaan seputar Islam (aqidah, fiqih, akhlak, sejarah Islam, tafsir, hadist, dll).
2. Setiap jawaban WAJIB menyertakan minimal satu dalil dari Al-Quran atau Hadist yang relevan.
3. Sebutkan sumber dalil dengan jelas (nama surah + nomor ayat, atau nama kitab hadist + perawi).
4. Jika ada lafaz Arab, tuliskan teks Arabnya di baris tersendiri, lalu terjemahannya di baris berikutnya.
5. Gunakan bahasa Indonesia yang sopan, ilmiah, dan mudah dipahami.
6. Tolak pertanyaan di luar topik Islam dengan sopan.
7. Awali jawaban dengan salam islami singkat.
8. Akhiri jawaban dengan motivasi islami singkat.

FORMAT WAJIB:
- Pisahkan setiap paragraf dengan SATU baris kosong.
- Gunakan penomoran (1. 2. 3.) untuk daftar.
- Untuk dalil, format setiap bagian di baris baru:

[teks Arab]
[transliterasi latin]
Artinya: [terjemahan]
(QS. NamaSurah: Ayat) atau (HR. Perawi)

- JANGAN gunakan **, ##, --, atau simbol markdown apapun.
- Teks Arab HARUS di baris tersendiri, dipisah baris kosong dari teks lain."""

MAX_HISTORY = 10
ADMIN_PW    = os.environ.get("ADMIN_PASSWORD", "admin123")


@app.route("/")
def home():
    if "sesi_id" not in session:
        session["sesi_id"] = os.urandom(8).hex()
    session["riwayat"] = []
    return render_template("index.html")


@app.route("/tanya-stream", methods=["POST"])
def tanya_stream():
    data       = request.json or {}
    pertanyaan = data.get("pertanyaan", "").strip()

    if not pertanyaan:
        return jsonify({"error": "Pertanyaan kosong"}), 400
    if len(pertanyaan) > 1000:
        return jsonify({"error": "Pertanyaan terlalu panjang"}), 400

    if "riwayat" not in session:
        session["riwayat"] = []
    if "sesi_id" not in session:
        session["sesi_id"] = os.urandom(8).hex()

    riwayat = list(session["riwayat"])
    riwayat.append({"role": "user", "content": pertanyaan})
    if len(riwayat) > MAX_HISTORY:
        riwayat = riwayat[-MAX_HISTORY:]

    messages   = [{"role": "system", "content": SISTEM}] + riwayat
    sesi_id    = session["sesi_id"]

    def generate():
        jawaban_penuh = ""
        try:
            stream = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=messages,
                temperature=0.7,
                max_tokens=1200,
                stream=True
            )
            for chunk in stream:
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    jawaban_penuh += delta
                    yield f"data: {json.dumps({'token': delta})}\n\n"

            # Simpan ke DB setelah selesai
            with app.app_context():
                p = Percakapan(sesi_id=sesi_id, pertanyaan=pertanyaan, jawaban=jawaban_penuh)
                db.session.add(p)
                db.session.commit()
                row_id = p.id

            # Update session riwayat
            riwayat.append({"role": "assistant", "content": jawaban_penuh})
            session["riwayat"]  = riwayat
            session.modified    = True

            yield f"data: {json.dumps({'done': True, 'id': row_id})}\n\n"

        except Exception as e:
            logger.error(f"Stream error: {e}")
            yield f"data: {json.dumps({'error': 'Terjadi kesalahan, coba lagi.'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@app.route("/rating", methods=["POST"])
def rating():
    try:
        data  = request.json
        pid   = data.get("id")
        nilai = data.get("rating")
        if not pid or nilai not in [1, 2, 3, 4, 5]:
            return jsonify({"error": "Data tidak valid"}), 400
        p = Percakapan.query.get(pid)
        if p:
            p.rating = nilai
            db.session.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/reset", methods=["POST"])
def reset():
    session["riwayat"] = []
    session.modified   = True
    return jsonify({"status": "ok"})


@app.route("/admin")
def admin():
    pw = request.args.get("pw", "")
    if pw != ADMIN_PW:
        return "Akses ditolak. Tambahkan ?pw=PASSWORD di URL.", 403
    total       = Percakapan.query.count()
    rated       = Percakapan.query.filter(Percakapan.rating != None).count()
    avg_rating  = db.session.query(db.func.avg(Percakapan.rating)).scalar()
    terbaru     = Percakapan.query.order_by(Percakapan.waktu.desc()).limit(20).all()
    total_sesi  = db.session.query(db.func.count(db.func.distinct(Percakapan.sesi_id))).scalar()
    hari_ini    = Percakapan.query.filter(
                    db.func.date(Percakapan.waktu) == db.func.date(datetime.utcnow())
                  ).count()
    rating_dist = {i: Percakapan.query.filter_by(rating=i).count() for i in range(1, 6)}
    return render_template("admin.html",
        total=total, rated=rated,
        avg_rating=round(avg_rating, 2) if avg_rating else 0,
        terbaru=terbaru, rating_dist=rating_dist,
        total_sesi=total_sesi, hari_ini=hari_ini, pw=pw
    )


@app.route("/export-csv")
def export_csv():
    pw = request.args.get("pw", "")
    if pw != ADMIN_PW:
        return "Akses ditolak.", 403
    semua = Percakapan.query.order_by(Percakapan.waktu).all()
    lines = ["ID,Sesi,Pertanyaan,Jawaban,Rating,Waktu"]
    for p in semua:
        q = p.pertanyaan.replace('"', '""')
        a = p.jawaban.replace('"', '""')
        r = p.rating if p.rating else ""
        w = p.waktu.strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f'{p.id},"{p.sesi_id[:8]}","{q}","{a}",{r},"{w}"')
    return Response("\n".join(lines), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=data_pratamaai.csv"})


@app.route("/export-pdf")
def export_pdf():
    try:
        sesi_id = session.get("sesi_id")
        data    = Percakapan.query.filter_by(sesi_id=sesi_id).order_by(Percakapan.waktu).all()
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 10, "PratamaAI - Riwayat Percakapan", ln=True, align="C")
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 8, f"Diekspor: {datetime.now().strftime('%d/%m/%Y %H:%M')}", ln=True, align="C")
        pdf.cell(0, 6, "Dikembangkan oleh Muhammad Ibnu Setiawan Pratama", ln=True, align="C")
        pdf.ln(5)
        for i, p in enumerate(data, 1):
            pdf.set_font("Helvetica", "B", 11)
            pdf.set_fill_color(220, 240, 220)
            pdf.cell(0, 8, f"Pertanyaan {i}:", ln=True, fill=True)
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 6, p.pertanyaan)
            pdf.ln(2)
            pdf.set_font("Helvetica", "B", 11)
            pdf.set_fill_color(240, 240, 240)
            pdf.cell(0, 8, "Jawaban PratamaAI:", ln=True, fill=True)
            pdf.set_font("Helvetica", "", 10)
            clean = p.jawaban.encode("latin-1", "replace").decode("latin-1")
            pdf.multi_cell(0, 6, clean)
            if p.rating:
                pdf.set_font("Helvetica", "I", 9)
                pdf.cell(0, 6, f"Rating: {p.rating}/5", ln=True)
            pdf.ln(4)
            pdf.set_draw_color(200, 200, 200)
            pdf.line(10, pdf.get_y(), 200, pdf.get_y())
            pdf.ln(4)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        pdf.output(tmp.name)
        return send_file(tmp.name, as_attachment=True, download_name="riwayat_pratamaai.pdf")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True)