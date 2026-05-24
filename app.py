from flask import Flask, render_template, request, jsonify, session, send_file, Response, stream_with_context, redirect, url_for
from groq import Groq
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from fpdf import FPDF
import os, logging, tempfile, json
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "pratama-secret-key")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///pratama.db")
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
    nama_user  = db.Column(db.String(100))
    peran_user = db.Column(db.String(50))
    pertanyaan = db.Column(db.Text)
    jawaban    = db.Column(db.Text)
    rating     = db.Column(db.Integer, nullable=True)
    is_kuis    = db.Column(db.Boolean, default=False)
    topik_kuis = db.Column(db.String(100), nullable=True)
    level_kuis = db.Column(db.String(50), nullable=True)
    skor_kuis  = db.Column(db.Integer, nullable=True)
    waktu      = db.Column(db.DateTime, default=datetime.utcnow)

class AdminUser(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    username   = db.Column(db.String(80), unique=True, nullable=False)
    password   = db.Column(db.String(200), nullable=False)
    role       = db.Column(db.String(20), default='admin')  # 'owner' atau 'admin'
    dibuat     = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    db.create_all()
    # Auto-migrate: tambah kolom baru jika belum ada di database lama
    from sqlalchemy import text
    kolom_baru = [
        ("nama_user",  "ALTER TABLE percakapan ADD COLUMN nama_user VARCHAR(100)"),
        ("peran_user", "ALTER TABLE percakapan ADD COLUMN peran_user VARCHAR(50)"),
        ("is_kuis",    "ALTER TABLE percakapan ADD COLUMN is_kuis BOOLEAN DEFAULT 0"),
        ("topik_kuis", "ALTER TABLE percakapan ADD COLUMN topik_kuis VARCHAR(100)"),
        ("level_kuis", "ALTER TABLE percakapan ADD COLUMN level_kuis VARCHAR(50)"),
        ("skor_kuis",  "ALTER TABLE percakapan ADD COLUMN skor_kuis INTEGER"),
    ]
    for nama_kolom, sql in kolom_baru:
        try:
            with db.engine.connect() as conn:
                conn.execute(text(sql))
                conn.commit()
                logger.info(f"Kolom '{nama_kolom}' berhasil ditambahkan.")
        except Exception:
            pass  # Kolom sudah ada, abaikan

    # Auto-create owner default jika belum ada
    try:
        owner_username = os.environ.get("OWNER_USERNAME", "owner")
        owner_password = os.environ.get("OWNER_PASSWORD", "owner123")
        existing = AdminUser.query.filter_by(role='owner').first()
        if not existing:
            owner = AdminUser(
                username = owner_username,
                password = generate_password_hash(owner_password),
                role     = 'owner'
            )
            db.session.add(owner)
            db.session.commit()
            logger.info(f"Akun owner '{owner_username}' berhasil dibuat.")
        else:
            logger.info(f"Owner sudah ada: '{existing.username}'")
    except Exception as e:
        db.session.rollback()
        logger.error(f"Gagal buat owner: {e}")

SISTEM_DASAR = """Kamu adalah PratamaAI, asisten pembelajaran Pendidikan Agama Islam (PAI) yang dikembangkan oleh Muhammad Ibnu Setiawan Pratama.

IDENTITAS DIRI:
- Nama: PratamaAI
- Pengembang: Muhammad Ibnu Setiawan Pratama (Mahasiswa S2 PAI)
- Perkenalkan diri HANYA jika ditanya siapa kamu.

ATURAN MENJAWAB:
1. Jawab HANYA pertanyaan seputar Islam (aqidah, fiqih, akhlak, sejarah Islam, tafsir, hadist, dll).
2. Setiap jawaban WAJIB menyertakan minimal satu dalil dari Al-Quran atau Hadist yang relevan.
3. Sebutkan sumber dalil dengan jelas (nama surah + nomor ayat, atau nama kitab hadist + perawi).
4. Jika ada lafaz Arab, tuliskan teks Arabnya di baris tersendiri, lalu transliterasi latin, lalu terjemahannya.
5. Gunakan bahasa Indonesia yang sopan, ilmiah, dan mudah dipahami.
6. Tolak pertanyaan di luar topik Islam dengan sopan.
7. Akhiri jawaban dengan motivasi islami singkat.

ATURAN SALAM:
- Jika sudah_salam = True: JANGAN gunakan salam pembuka lagi. Boleh mulai dengan Bismillah atau langsung jawab.
- Jika sudah_salam = False: Gunakan salam islami lengkap di awal.

FORMAT WAJIB:
- Pisahkan setiap paragraf dengan SATU baris kosong.
- Gunakan penomoran (1. 2. 3.) untuk daftar.
- Format dalil Al-Quran:
[teks Arab]
[transliterasi latin]
Artinya: [terjemahan]
(QS. NamaSurah: Ayat)
- Format Hadist:
[teks Arab jika ada]
Artinya: [terjemahan]
(HR. Perawi)
- JANGAN gunakan **, ##, atau simbol markdown apapun."""

MAX_HISTORY = 10

def admin_required(f):
    """Decorator: cek apakah sudah login sebagai admin/owner."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated

def owner_required(f):
    """Decorator: cek apakah sudah login sebagai owner."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        if session.get("admin_role") != "owner":
            return jsonify({"error": "Hanya owner yang bisa melakukan ini."}), 403
        return f(*args, **kwargs)
    return decorated


@app.route("/")
def home():
    if "sesi_id" not in session:
        session["sesi_id"] = os.urandom(8).hex()
    session["riwayat"] = []
    return render_template("index.html")


@app.route("/setup-owner")
def setup_owner():
    """Route darurat untuk buat akun owner — hapus setelah berhasil login."""
    kunci = request.args.get("kunci", "")
    if kunci != os.environ.get("SETUP_KEY", "pratama2026"):
        return "Akses ditolak. Tambahkan ?kunci=SETUP_KEY", 403
    try:
        with app.app_context():
            db.create_all()
            existing = AdminUser.query.filter_by(role='owner').first()
            if existing:
                return f"✅ Owner sudah ada: '{existing.username}'. Silakan login di <a href='/admin/login'>/admin/login</a>"
            owner_username = os.environ.get("OWNER_USERNAME", "owner")
            owner_password = os.environ.get("OWNER_PASSWORD", "owner123")
            owner = AdminUser(
                username = owner_username,
                password = generate_password_hash(owner_password),
                role     = 'owner'
            )
            db.session.add(owner)
            db.session.commit()
            return f"✅ Akun owner '{owner_username}' berhasil dibuat! Silakan <a href='/admin/login'>login di sini</a>."
    except Exception as e:
        return f"❌ Error: {str(e)}", 500


@app.route("/tanya-stream", methods=["POST"])
def tanya_stream():
    data        = request.json or {}
    pertanyaan  = data.get("pertanyaan", "").strip()
    user_data   = data.get("user_data", {})
    sudah_salam = data.get("sudah_salam", False)
    is_kuis     = data.get("is_kuis", False)
    topik_kuis  = data.get("topik_kuis", None)
    level_kuis  = data.get("level_kuis", None)
    skor_kuis   = data.get("skor_kuis", None)

    if not pertanyaan:
        return jsonify({"error": "Pertanyaan kosong"}), 400
    if len(pertanyaan) > 2000:
        return jsonify({"error": "Pertanyaan terlalu panjang"}), 400

    if "riwayat" not in session:
        session["riwayat"] = []
    if "sesi_id" not in session:
        session["sesi_id"] = os.urandom(8).hex()

    riwayat = list(session["riwayat"])
    riwayat.append({"role": "user", "content": pertanyaan})
    if len(riwayat) > MAX_HISTORY:
        riwayat = riwayat[-MAX_HISTORY:]

    sistem = SISTEM_DASAR + f"\n\nsudah_salam = {sudah_salam}"

    if user_data and user_data.get("nama"):
        nama  = user_data.get("nama", "")
        peran = user_data.get("peran", "")
        extra = user_data.get("extra", "")
        info  = f"\n\nINFO PENGGUNA:\n- Nama: {nama}\n- Peran: {peran}"
        if extra:
            info += f"\n- Institusi: {extra}"
        sistem += info

    messages   = [{"role": "system", "content": sistem}] + riwayat
    sesi_id    = session["sesi_id"]
    nama_user  = user_data.get("nama", "Anonim") if user_data else "Anonim"
    peran_user = user_data.get("peran", "") if user_data else ""

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

            with app.app_context():
                p = Percakapan(
                    sesi_id    = sesi_id,
                    nama_user  = nama_user,
                    peran_user = peran_user,
                    pertanyaan = pertanyaan,
                    jawaban    = jawaban_penuh,
                    is_kuis    = is_kuis,
                    topik_kuis = topik_kuis if is_kuis else None,
                    level_kuis = level_kuis if is_kuis else None,
                    skor_kuis  = skor_kuis if is_kuis else None
                )
                db.session.add(p)
                db.session.commit()
                row_id = p.id

            riwayat.append({"role": "assistant", "content": jawaban_penuh})
            session["riwayat"] = riwayat
            session.modified   = True

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
        if not pid or nilai not in [1,2,3,4,5]:
            return jsonify({"error": "Data tidak valid"}), 400
        p = db.session.get(Percakapan, pid)
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


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        user = AdminUser.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            session["admin_logged_in"] = True
            session["admin_username"]  = user.username
            session["admin_role"]      = user.role
            session.permanent          = False  # sampai browser ditutup
            return redirect(url_for("admin"))
        else:
            error = "Username atau password salah."
    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_logged_in", None)
    session.pop("admin_username", None)
    session.pop("admin_role", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin():
    try:
        from sqlalchemy import func
        total       = Percakapan.query.count()
        rated       = Percakapan.query.filter(Percakapan.rating != None).count()
        avg_rating  = db.session.query(db.func.avg(Percakapan.rating)).scalar()
        terbaru     = Percakapan.query.order_by(Percakapan.waktu.desc()).limit(20).all()
        total_sesi  = db.session.query(db.func.count(db.func.distinct(Percakapan.sesi_id))).scalar()
        hari_ini    = Percakapan.query.filter(
                        db.func.date(Percakapan.waktu) == db.func.date(datetime.utcnow())
                      ).count()
        rating_dist = {i: Percakapan.query.filter_by(rating=i).count() for i in range(1,6)}
        pengguna_list = db.session.query(
            Percakapan.nama_user, Percakapan.peran_user,
            func.count(Percakapan.id).label('jumlah_chat'),
            func.max(Percakapan.waktu).label('terakhir_aktif'),
            func.count(db.case((Percakapan.is_kuis == True, 1))).label('jumlah_kuis')
        ).group_by(Percakapan.nama_user, Percakapan.peran_user)\
         .order_by(func.max(Percakapan.waktu).desc()).all()
        total_kuis     = Percakapan.query.filter_by(is_kuis=True).count()
        kuis_terbaru   = Percakapan.query.filter_by(is_kuis=True)\
                          .order_by(Percakapan.waktu.desc()).limit(20).all()
        kuis_per_topik = db.session.query(
            Percakapan.topik_kuis,
            func.count(Percakapan.id).label('jumlah')
        ).filter(Percakapan.is_kuis==True, Percakapan.topik_kuis!=None)\
         .group_by(Percakapan.topik_kuis).all()

        # Daftar admin (hanya untuk owner)
        daftar_admin = AdminUser.query.filter_by(role='admin').all() if session.get("admin_role") == "owner" else []

        return render_template("admin.html",
            total=total, rated=rated,
            avg_rating=round(avg_rating,2) if avg_rating else 0,
            terbaru=terbaru, rating_dist=rating_dist,
            total_sesi=total_sesi, hari_ini=hari_ini,
            pengguna_list=pengguna_list,
            total_kuis=total_kuis, kuis_terbaru=kuis_terbaru,
            kuis_per_topik=kuis_per_topik,
            admin_username=session.get("admin_username"),
            admin_role=session.get("admin_role"),
            daftar_admin=daftar_admin
        )
    except Exception as e:
        logger.error(f"Admin error: {e}")
        return f"Error admin: {str(e)}", 500


# ── Manajemen Admin (hanya owner) ──────────────────────────────

@app.route("/admin/tambah-admin", methods=["POST"])
@owner_required
def tambah_admin():
    data     = request.json or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return jsonify({"error": "Username dan password wajib diisi."}), 400
    if AdminUser.query.filter_by(username=username).first():
        return jsonify({"error": "Username sudah digunakan."}), 409
    admin = AdminUser(
        username = username,
        password = generate_password_hash(password),
        role     = "admin"
    )
    db.session.add(admin)
    db.session.commit()
    return jsonify({"status": "ok", "id": admin.id})


@app.route("/admin/hapus-admin", methods=["POST"])
@owner_required
def hapus_admin():
    data     = request.json or {}
    admin_id = data.get("id")
    user     = AdminUser.query.get(admin_id)
    if not user:
        return jsonify({"error": "Admin tidak ditemukan."}), 404
    if user.role == "owner":
        return jsonify({"error": "Owner tidak bisa dihapus."}), 403
    db.session.delete(user)
    db.session.commit()
    return jsonify({"status": "ok"})


@app.route("/simpan-kuis", methods=["POST"])
def simpan_kuis():
    try:
        data       = request.json or {}
        sesi_id    = session.get("sesi_id", os.urandom(8).hex())
        p = Percakapan(
            sesi_id    = sesi_id,
            nama_user  = data.get("nama_user", "Anonim"),
            peran_user = data.get("peran_user", ""),
            pertanyaan = data.get("soal", ""),
            jawaban    = data.get("jawaban_siswa", ""),
            is_kuis    = True,
            topik_kuis = data.get("topik", None),
            level_kuis = data.get("level", None),
            skor_kuis  = data.get("skor", None)
        )
        db.session.add(p)
        db.session.commit()
        return jsonify({"status": "ok", "id": p.id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/edit/<int:pid>", methods=["POST"])
@admin_required
def edit(pid):
    p = db.session.get(Percakapan, pid)
    if not p:
        return jsonify({"error": "Data tidak ditemukan"}), 404
    data = request.json or {}
    if "pertanyaan" in data: p.pertanyaan = data["pertanyaan"]
    if "jawaban"    in data: p.jawaban    = data["jawaban"]
    if "nama_user"  in data: p.nama_user  = data["nama_user"]
    if "rating"     in data: p.rating     = data["rating"]
    db.session.commit()
    return jsonify({"status": "ok"})


@app.route("/export-csv")
@admin_required
def export_csv():
    semua = Percakapan.query.order_by(Percakapan.waktu).all()
    lines = ["ID,Sesi,Nama,Peran,Jenis,Topik Kuis,Level Kuis,Skor Kuis,Pertanyaan,Jawaban,Rating,Waktu"]
    for p in semua:
        q     = p.pertanyaan.replace('"','""')
        a     = p.jawaban.replace('"','""')
        r     = p.rating if p.rating else ""
        w     = p.waktu.strftime("%Y-%m-%d %H:%M:%S")
        jenis = "Kuis" if p.is_kuis else "Chat"
        topik = p.topik_kuis or ""
        level = p.level_kuis or ""
        skor  = p.skor_kuis if p.skor_kuis is not None else ""
        lines.append(f'{p.id},"{p.sesi_id[:8]}","{p.nama_user}","{p.peran_user}","{jenis}","{topik}","{level}",{skor},"{q}","{a}",{r},"{w}"')
    return Response("\n".join(lines), mimetype="text/csv",
        headers={"Content-Disposition":"attachment; filename=data_pratamaai.csv"})


@app.route("/export-pdf")
def export_pdf():
    try:
        sesi_id = session.get("sesi_id")
        data    = Percakapan.query.filter_by(sesi_id=sesi_id).order_by(Percakapan.waktu).all()
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica","B",16)
        pdf.cell(0,10,"PratamaAI - Riwayat Percakapan",ln=True,align="C")
        pdf.set_font("Helvetica","",10)
        pdf.cell(0,8,f"Diekspor: {datetime.now().strftime('%d/%m/%Y %H:%M')}",ln=True,align="C")
        pdf.cell(0,6,"Dikembangkan oleh Muhammad Ibnu Setiawan Pratama",ln=True,align="C")
        pdf.ln(5)
        for i,p in enumerate(data,1):
            pdf.set_font("Helvetica","B",11)
            pdf.set_fill_color(220,240,220)
            pdf.cell(0,8,f"Pertanyaan {i}:",ln=True,fill=True)
            pdf.set_font("Helvetica","",10)
            pdf.multi_cell(0,6,p.pertanyaan)
            pdf.ln(2)
            pdf.set_font("Helvetica","B",11)
            pdf.set_fill_color(240,240,240)
            pdf.cell(0,8,"Jawaban PratamaAI:",ln=True,fill=True)
            pdf.set_font("Helvetica","",10)
            clean = p.jawaban.encode("latin-1","replace").decode("latin-1")
            pdf.multi_cell(0,6,clean)
            if p.rating:
                pdf.set_font("Helvetica","I",9)
                pdf.cell(0,6,f"Rating: {p.rating}/5",ln=True)
            pdf.ln(4)
            pdf.set_draw_color(200,200,200)
            pdf.line(10,pdf.get_y(),200,pdf.get_y())
            pdf.ln(4)
        tmp = tempfile.NamedTemporaryFile(delete=False,suffix=".pdf")
        pdf.output(tmp.name)
        return send_file(tmp.name,as_attachment=True,download_name="riwayat_pratamaai.pdf")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/pengguna-riwayat")
@admin_required
def pengguna_riwayat():
    nama = request.args.get("nama", "")
    data = Percakapan.query.filter_by(nama_user=nama).order_by(Percakapan.waktu.desc()).all()
    hasil = []
    for p in data:
        hasil.append({
            "id": p.id,
            "pertanyaan": p.pertanyaan,
            "jawaban": p.jawaban[:300] + "..." if len(p.jawaban) > 300 else p.jawaban,
            "rating": p.rating,
            "is_kuis": p.is_kuis,
            "topik_kuis": p.topik_kuis,
            "level_kuis": p.level_kuis,
            "skor_kuis": p.skor_kuis,
            "waktu": p.waktu.strftime("%d/%m/%Y %H:%M")
        })
    return jsonify(hasil)


@app.route("/hapus-pengguna", methods=["POST"])
@admin_required
def hapus_pengguna():
    nama = (request.json or {}).get("nama", "")
    if not nama:
        return jsonify({"error": "Nama tidak boleh kosong"}), 400
    Percakapan.query.filter_by(nama_user=nama).delete()
    db.session.commit()
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True)