from flask import Flask, render_template, request, jsonify, session, send_file
from groq import Groq
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from fpdf import FPDF
import os, logging, tempfile
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

# === MODEL DATABASE ===
class Percakapan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sesi_id = db.Column(db.String(100))
    pertanyaan = db.Column(db.Text)
    jawaban = db.Column(db.Text)
    rating = db.Column(db.Integer, nullable=True)
    waktu = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    db.create_all()

SISTEM = """Kamu adalah PratamaAI, asisten pembelajaran Pendidikan Agama Islam (PAI) yang dikembangkan oleh Muhammad Ibnu Setiawan Pratama sebagai bagian dari penelitian tesis S2 PAI.

IDENTITAS DIRI:
- Nama: PratamaAI
- Pengembang: Muhammad Ibnu Setiawan Pratama (Mahasiswa S2 PAI)
- Perkenalkan diri HANYA jika ditanya siapa kamu atau saat pertama kali sapa. Jangan perkenalkan diri di setiap jawaban.

ATURAN MENJAWAB:
1. Jawab HANYA pertanyaan seputar Islam (aqidah, fiqih, akhlak, sejarah Islam, tafsir, hadist, dll).
2. Setiap jawaban WAJIB menyertakan minimal satu dalil dari Al-Quran atau Hadist yang relevan.
3. Sebutkan sumber dalil dengan jelas (nama surah + nomor ayat, atau nama kitab hadist + perawi).
4. Jika ada lafaz Arab, tuliskan teks Arabnya terlebih dahulu, lalu terjemahannya.
5. Gunakan bahasa Indonesia yang sopan, ilmiah, dan mudah dipahami.
6. Tolak pertanyaan di luar topik Islam dengan sopan.
7. Awali jawaban dengan salam islami yang singkat dan tidak berulang.
8. Akhiri jawaban dengan motivasi islami singkat.

FORMAT JAWABAN:
- Gunakan paragraf yang rapi dengan baris kosong antar paragraf.
- Gunakan penomoran (1. 2. 3.) jika menjelaskan beberapa hal.
- Format dalil Al-Quran: (teks Arab)\n(Terjemahan) — QS. NamaSurah: Ayat
- Format Hadist: (teks Arab jika ada)\n(Terjemahan) — HR. Perawi
- JANGAN gunakan simbol **, ##, atau markdown lainnya.
- Pisahkan paragraf dengan baris kosong."""

MAX_HISTORY = 10

@app.route("/")
def home():
    if "sesi_id" not in session:
        session["sesi_id"] = os.urandom(8).hex()
    session["riwayat"] = []
    return render_template("index.html")

@app.route("/tanya", methods=["POST"])
def tanya():
    try:
        data = request.json
        pertanyaan = data.get("pertanyaan", "").strip()
        if not pertanyaan:
            return jsonify({"error": "Pertanyaan kosong"}), 400
        if len(pertanyaan) > 1000:
            return jsonify({"error": "Pertanyaan terlalu panjang"}), 400

        if "riwayat" not in session:
            session["riwayat"] = []
        if "sesi_id" not in session:
            session["sesi_id"] = os.urandom(8).hex()

        riwayat = session["riwayat"]
        riwayat.append({"role": "user", "content": pertanyaan})
        if len(riwayat) > MAX_HISTORY:
            riwayat = riwayat[-MAX_HISTORY:]

        messages = [{"role": "system", "content": SISTEM}] + riwayat
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.7,
            max_tokens=1024
        )
        jawaban = response.choices[0].message.content
        riwayat.append({"role": "assistant", "content": jawaban})
        session["riwayat"] = riwayat
        session.modified = True

        # Simpan ke database
        percakapan = Percakapan(
            sesi_id=session["sesi_id"],
            pertanyaan=pertanyaan,
            jawaban=jawaban
        )
        db.session.add(percakapan)
        db.session.commit()

        return jsonify({"jawaban": jawaban, "id": percakapan.id})

    except Exception as e:
        logger.error(f"Error: {str(e)}")
        return jsonify({"error": "Terjadi kesalahan. Silakan coba lagi."}), 500

@app.route("/rating", methods=["POST"])
def rating():
    try:
        data = request.json
        percakapan_id = data.get("id")
        nilai = data.get("rating")
        if not percakapan_id or nilai not in [1,2,3,4,5]:
            return jsonify({"error": "Data tidak valid"}), 400
        percakapan = Percakapan.query.get(percakapan_id)
        if percakapan:
            percakapan.rating = nilai
            db.session.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/reset", methods=["POST"])
def reset():
    session["riwayat"] = []
    session.modified = True
    return jsonify({"status": "ok"})

@app.route("/admin")
def admin():
    total = Percakapan.query.count()
    rated = Percakapan.query.filter(Percakapan.rating != None).count()
    avg_rating = db.session.query(db.func.avg(Percakapan.rating)).scalar()
    terbaru = Percakapan.query.order_by(Percakapan.waktu.desc()).limit(20).all()
    rating_dist = {}
    for i in range(1, 6):
        rating_dist[i] = Percakapan.query.filter_by(rating=i).count()
    return render_template("admin.html",
        total=total, rated=rated,
        avg_rating=round(avg_rating, 2) if avg_rating else 0,
        terbaru=terbaru, rating_dist=rating_dist
    )

@app.route("/export-pdf")
def export_pdf():
    try:
        sesi_id = session.get("sesi_id")
        data = Percakapan.query.filter_by(sesi_id=sesi_id).order_by(Percakapan.waktu).all()

        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 10, "PratamaAI - Riwayat Percakapan", ln=True, align="C")
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 8, f"Diekspor: {datetime.now().strftime('%d/%m/%Y %H:%M')}", ln=True, align="C")
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
            clean = p.jawaban.encode('latin-1', 'replace').decode('latin-1')
            pdf.multi_cell(0, 6, clean)
            if p.rating:
                pdf.set_font("Helvetica", "I", 9)
                pdf.cell(0, 6, f"Rating: {'★' * p.rating}", ln=True)
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