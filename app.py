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
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32).hex()
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
_db_url = os.environ.get("DATABASE_URL", "sqlite:///pratama.db")
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 300}
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def ambil_kunci_groq():
    raw = os.environ.get("GROQ_API_KEYS") or os.environ.get("GROQ_API_KEY") or ""
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    return keys

GROQ_MODELS = [
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
    "allam-2-7b"
]

def stream_ai_multi(messages):
    """
    Smart Multi-AI Orchestrator:
    1. Rotasi & Fallback Multi-Key Groq (Akun 1 -> Akun 2 -> dst).
    2. Multi-Model Fallback Groq (Qwen 27b -> GPT 120b -> Allam 7b).
    3. Emergency Backup Provider: Google Gemini API.
    """
    groq_keys = ambil_kunci_groq()
    for k_idx, key in enumerate(groq_keys):
        try:
            client_instance = Groq(api_key=key)
        except Exception:
            continue
        for model in GROQ_MODELS:
            try:
                stream = client_instance.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.3,
                    max_tokens=3000,
                    stream=True
                )
                emitted = False
                for chunk in stream:
                    delta = chunk.choices[0].delta.content or ""
                    if delta:
                        emitted = True
                        yield delta
                if emitted:
                    return
            except Exception as e:
                logger.warning(f"Groq Key #{k_idx+1} Model {model} limit/gagal: {e}. Beralih ke cadangan...")
                continue

    # Fallback darurat ke Google Gemini jika seluruh Groq limit
    gemini_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if gemini_key:
        try:
            import urllib.request
            logger.info("Mengaktifkan Fallback Emergency: Google Gemini API...")
            sys_text = ""
            user_texts = []
            for m in messages:
                if m.get("role") == "system":
                    sys_text += m.get("content", "") + "\n\n"
                elif m.get("role") == "user":
                    user_texts.append(m.get("content", ""))
                elif m.get("role") == "assistant":
                    user_texts.append("(Asisten sebelumnya): " + m.get("content", ""))

            full_prompt = (sys_text + "\n\nPertanyaan: " + "\n".join(user_texts)).strip()
            for g_model in ["gemini-flash-latest", "gemini-3.8-flash", "gemini-pro-latest"]:
                try:
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{g_model}:generateContent?key={gemini_key}"
                    payload = {"contents": [{"parts": [{"text": full_prompt}]}]}
                    req = urllib.request.Request(
                        url,
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"}
                    )
                    with urllib.request.urlopen(req, timeout=12) as resp:
                        g_data = json.loads(resp.read().decode("utf-8"))
                        g_cand = g_data.get("candidates", [{}])[0]
                        g_text = g_cand.get("content", {}).get("parts", [{}])[0].get("text", "")
                        if g_text:
                            yield g_text
                            return
                except Exception as ge:
                    logger.warning(f"Gemini model {g_model} gagal: {ge}")
                    continue
        except Exception as err:
            logger.error(f"Seluruh fallback Gemini gagal: {err}")

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

    # Akun owner dari environment variable (tanpa password default)
    # RESET_OWNER=1 -> paksa owner yang sudah ada mengikuti OWNER_USERNAME/OWNER_PASSWORD
    try:
        owner_username = (os.environ.get("OWNER_USERNAME") or "owner").strip()
        owner_password = (os.environ.get("OWNER_PASSWORD") or "").strip()
        reset_owner    = (os.environ.get("RESET_OWNER") or "").strip() == "1"
        existing = AdminUser.query.filter_by(role='owner').first()
        if existing and reset_owner and owner_password:
            existing.username = owner_username
            existing.password = generate_password_hash(owner_password)
            db.session.commit()
            logger.info(f"Owner di-reset. Username sekarang: '{owner_username}'. Hapus RESET_OWNER dari Environment.")
        elif existing:
            logger.info(f"Owner sudah ada dengan username: '{existing.username}'")
        elif not owner_password:
            logger.warning("OWNER_PASSWORD belum di-set, akun owner TIDAK dibuat.")
        else:
            owner = AdminUser(
                username = owner_username,
                password = generate_password_hash(owner_password),
                role     = 'owner'
            )
            db.session.add(owner)
            db.session.commit()
            logger.info(f"Akun owner '{owner_username}' berhasil dibuat.")
    except Exception as e:
        db.session.rollback()
        logger.error(f"Gagal buat owner: {e}")

SISTEM_DASAR = """Kamu adalah PratamaAI, asisten pembelajaran Pendidikan Agama Islam (PAI) berbasis kecerdasan buatan.

IDENTITAS & PENGEMBANG:
- Nama AI: PratamaAI
- Pengembang: Muhamad Ibnu Setiawan Pratama
- Profil Pengembang:
  * Mahasiswa Program Magister (S2) Pendidikan Agama Islam (PAI) di UIN Sultan Maulana Hasanuddin Banten (UIN Banten).
  * Peneliti dan akademisi muda yang aktif mempublikasikan berbagai karya ilmiah dan memiliki portofolio riset di bidang inovasi media pembelajaran berbasis teknologi, mobile learning, serta integrasi Artificial Intelligence (AI) dalam transformasi pendidikan Islam.
  * PratamaAI dikembangkan sebagai bagian dari riset inovasi tesis magister (S2) beliau untuk menghadirkan asisten cerdas PAI yang interaktif, berbobot ilmiah, dan berlandaskan dalil otentik Al-Qur'an dan Hadist.

ATURAN MEMPERKENALKAN DIRI:
- Perkenalkan diri dan profil pengembang HANYA jika pengguna bertanya seputar: siapa kamu, siapa pembuatmu, siapa pengembangmu, deskripsi tentang AI ini, atau profil Muhamad Ibnu Setiawan Pratama.
- Sampaikan profil pengembang dengan bahasa yang santun, ilmiah, dan membanggakan, menegaskan status beliau sebagai mahasiswa S2 PAI UIN Banten beserta rekam jejak riset, publikasi ilmiah, dan portofolio teknologi pendidikannya.

ATURAN MENJAWAB & KEOTENTIKAN DALIL:
1. Jawab HANYA pertanyaan seputar Islam (aqidah, fiqih, akhlak, sejarah Islam, tafsir, hadist, dll) atau pertanyaan mengenai identitas PratamaAI dan pengembangnya.
2. JAWABAN WAJIB MEMILIKI PENJELASAN MATERI LENGKAP & BERBOBOT EDUKATIF:
   - DILARANG HANYA MENGUTIP DALIL SAJA TANPA PENJELASAN! Kamu adalah asisten guru/dosen PAI, pengguna membutuhkan pemahaman konsep, latar belakang hukum, rincian fiqih, dan hikmahnya.
   - Uraikan jawaban secara komprehensif: jelaskan pengertiannya, status hukumnya, rukun/syarat/sebabnya, pandangan ulama/madzhab jika relevan, baru sertakan dalil naqli Al-Qur'an dan Hadist sebagai penguat.
3. KEOTENTIKAN DALIL AL-QUR'AN (SANGAT KRUSIAL - DILARANG HALUSINASI):
   - DILARANG KERAS MENGARANG ATAU MERANGKAI SENDIRI TEKS ARAB DAN NOMOR AYAT AL-QUR'AN! Teks Arab Al-Qur'an adalah kalamullah yang suci, setiap kata dan harakat harus mutawatir.
   - Nama surah dan nomor ayat WAJIB tepat sesuai mushaf standar Kemenag RI.
   - KAMUS RUJUKAN DALIL POKOK MATERI PAI & FIQIH (GUNAKAN INI SECARA TEPAT):
     * Hukum Membunuh / Jinayah / Menghilangkan Nyawa:
       - QS. Al-Ma'idah: 32 (Larangan membunuh satu manusia bagaikan membunuh seluruh manusia)
       - QS. Al-Isra: 33 (Larangan membunuh jiwa yang diharamkan Allah kecuali dengan hak)
       - QS. An-Nisa: 93 (Balasan neraka jahannam bagi pembunuh orang beriman secara sengaja)
       - QS. An-Nisa: 29 (Larangan memakan harta sesama secara batil dan larangan bunuh diri)
       - (PERINGATAN KERAS: JANGAN PERNAH sebut Al-Baqarah untuk dalil larangan membunuh!)
     * Hukum Zina, Aurat, & Pergaulan:
       - QS. Al-Isra: 32 (وَلَا تَقْرَبُوا الزِّنَىٰ ۖ إِنَّهُ كَانَ فَاحِشَةً وَسَاءَ سَبِيلًا - Larangan mendekati zina)
       - QS. An-Nur: 2 (Hukuman dera 100 kali bagi pezina ghairu muhshan)
       - QS. An-Nur: 30 (Perintah menundukkan pandangan & menjaga kemaluan bagi laki-laki mukmin)
       - QS. An-Nur: 31 (Perintah menundukkan pandangan, menjaga kemaluan & menutup jilbab bagi wanita mukminah)
       - QS. Al-Ahzab: 59 (Perintah mengulurkan jilbab ke seluruh tubuh)
       - (PERINGATAN KERAS: JANGAN PERNAH menisbatkan larangan zina ke QS. Al-Ahzab: 33!)
     * Ibadah Pokok (Rukun Islam):
       - Kewajiban Puasa Ramadhan: QS. Al-Baqarah: 183
       - Penjelasan Bulan Ramadhan & Turunnya Al-Qur'an: QS. Al-Baqarah: 185
       - Kewajiban Shalat & Menunaikan Zakat: QS. Al-Baqarah: 43
       - Waktu-Waktu Shalat: QS. Al-Isra: 78
       - Kewajiban Haji bagi yang Mampu: QS. Ali 'Imran: 97
     * Muamalah & Harta:
       - Larangan Riba & Halalnya Jual Beli: QS. Al-Baqarah: 275
       - Perintah Pencatatan Hutang: QS. Al-Baqarah: 282
       - Pembagian Harta Waris (Faraidh): QS. An-Nisa: 11 dan QS. An-Nisa: 12
       - Larangan Mengurangi Timbangan / Curang: QS. Al-Mutaffifin: 1-3
       - Hukuman Pencurian: QS. Al-Ma'idah: 38
     * Pernikahan (Munakahat):
       - Tujuan Pernikahan (Sakinah, Mawaddah, Rahmah): QS. Ar-Rum: 21
       - Pergaulan Suami Istri yang Baik: QS. An-Nisa: 19
     * Akhlak, Toleransi & Akidah:
       - Berbakti kepada Orang Tua (Birrul Walidain): QS. Al-Isra: 23-24
       - Larangan Ghibah & Mencari Kesalahan Orang: QS. Al-Hujurat: 12
       - Persaudaraan Antar Mukmin: QS. Al-Hujurat: 10
       - Toleransi Beragama: QS. Al-Kafirun: 1-6 dan QS. Al-Mumtahanah: 8
       - Menuntut Ilmu: QS. Al-Mujadilah: 11
       - Sabar & Shalat sebagai Penolong: QS. Al-Baqarah: 153
       - Keesaan Allah SWT (Tauhid): QS. Al-Ikhlas: 1-4
   - Jika kamu ragu nomor ayat persisnya atau tidak hafal teks Arab aslinya secara harfiah, LEBIH BAIK sebutkan terjemahan maknanya atau gunakan Hadist Sahih daripada mengarang teks Arab palsu!
4. Jika ada lafaz Arab, tuliskan teks Arab otentiknya di baris tersendiri, lalu transliterasi latin, lalu terjemahannya.
5. Gunakan bahasa Indonesia yang sopan, ilmiah, dan mudah dipahami.
6. Tolak pertanyaan di luar topik Islam (kecuali pertanyaan tentang identitas AI/pengembang) dengan sopan.
7. Akhiri jawaban dengan motivasi islami singkat.

ATURAN SALAM:
- Jika sudah_salam = True: JANGAN gunakan salam pembuka lagi. Boleh mulai dengan Bismillah atau langsung jawab.
- Jika sudah_salam = False: Gunakan salam islami lengkap di awal.

STRUKTUR WAJIB JAWABAN PEMBELAJARAN PAI:
1. PENJELASAN KONSEP & HUKUM:
   - Buka dengan penjelasan definisi, kedudukan hukum dalam Islam, dan latar belakang materi yang ditanyakan secara lugas dan ilmiah.
2. URAIAN RINCIAN MATERI:
   - Jelaskan aspek fiqih, rukun, syarat, kategori, atau hikmah materi secara berbobot (2-3 paragraf penjelasan).
3. DALIL NAQLI PENDUKUNG (AL-QUR'AN & HADIST):
   - Letakkan dalil di posisi yang tepat untuk memperkuat penjelasan di atas dengan format baku:
[teks Arab asli]
[transliterasi latin]
Artinya: "[terjemahan bahasa Indonesia]"
QS. NamaSurah: NomorAyat
4. HIKMAH & PENUTUP:
   - Jelaskan hikmah pembelajaran (nilai tarbawi) bagi kehidupan kita hari ini, dan tutup dengan nasihat motivasi singkat.

ATURAN KETAT FORMAT SUMBER SURAH:
- Tuliskan HANYA satu nama surah dan nomor ayat pokok, contoh:
  QS. Al-Ma'idah: 32
  QS. Al-Isra: 32
  QS. Al-Baqarah: 183
  QS. An-Nur: 2
- DILARANG KERAS menulis nomor surah dalam kurung seperti "Surah Al-Baqarah (2) ayat 4.29" atau "Surah Al-Ma'idah (5) ayat 32"!
- DILARANG menulis nomor ayat memakai titik seperti "4.29" atau "2.183"!
- Format Hadist:
[teks Arab jika ada]
Artinya: "[terjemahan]"
HR. Perawi
- JANGAN gunakan simbol markdown seperti **, ##, atau ``` pada teks."""

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
            for token in stream_ai_multi(messages):
                jawaban_penuh += token
                yield f"data: {json.dumps({'token': token})}\n\n"

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


@app.route("/hitung-waris", methods=["POST"])
def hitung_waris():
    try:
        from fractions import Fraction
        data = request.json or {}
        tirkah_kotor  = data.get("total_harta", 0)
        hutang        = data.get("hutang", 0)
        biaya_jenazah = data.get("biaya_jenazah", 0)
        wasiat_input  = data.get("wasiat", 0)
        jenis_pewaris = data.get("jenis_kelamin_pewaris", "laki-laki")
        ahli          = data.get("ahli_waris", {})

        hasil = kalkulasi_faraidh(tirkah_kotor, hutang, biaya_jenazah, wasiat_input, jenis_pewaris, ahli)
        if hasil.get("status") == "error":
            return jsonify(hasil), 400
        return jsonify(hasil)
    except Exception as e:
        logger.error(f"Error hitung waris: {e}")
        return jsonify({"status": "error", "message": f"Terjadi kesalahan: {str(e)}"}), 500


def kalkulasi_faraidh(tirkah_kotor, hutang, biaya_jenazah, wasiat_input, jenis_pewaris, ahli):
    from fractions import Fraction
    tirkah_kotor = max(0.0, float(tirkah_kotor or 0))
    hutang = max(0.0, float(hutang or 0))
    biaya_jenazah = max(0.0, float(biaya_jenazah or 0))
    wasiat_input = max(0.0, float(wasiat_input or 0))

    kewajiban = hutang + biaya_jenazah
    sisa_setelah_kewajiban = max(0.0, tirkah_kotor - kewajiban)

    wasiat_maksimal = sisa_setelah_kewajiban / 3.0
    wasiat_disetujui = min(wasiat_input, wasiat_maksimal)
    catatan_wasiat = ""
    if wasiat_input > wasiat_maksimal:
        catatan_wasiat = f"Wasiat dibatasi maksimal 1/3 harta bersih (Rp {wasiat_maksimal:,.0f}) sesuai kaidah syariat."

    tirkah_bersih = max(0.0, sisa_setelah_kewajiban - wasiat_disetujui)
    if tirkah_bersih <= 0:
        return {
            "status": "error",
            "message": "Harta waris habis untuk melunasi hutang dan pengurusan jenazah. Tidak ada harta yang dibagikan."
        }

    suami = 1 if (jenis_pewaris == 'perempuan' and ahli.get('suami', 0) > 0) else 0
    istri = ahli.get('istri', 0) if jenis_pewaris == 'laki-laki' else 0
    istri = min(4, max(0, int(istri)))

    anak_laki = max(0, int(ahli.get('anak_laki', 0)))
    anak_puan = max(0, int(ahli.get('anak_perempuan', 0)))
    ayah = 1 if int(ahli.get('ayah', 0)) > 0 else 0
    ibu = 1 if int(ahli.get('ibu', 0)) > 0 else 0
    saudara_laki = max(0, int(ahli.get('saudara_laki', 0)))
    saudara_puan = max(0, int(ahli.get('saudara_perempuan', 0)))

    ada_anak = (anak_laki > 0 or anak_puan > 0)
    jml_saudara = saudara_laki + saudara_puan

    total_ahli = suami + istri + anak_laki + anak_puan + ayah + ibu + saudara_laki + saudara_puan
    if total_ahli == 0:
        return {"status": "error", "message": "Pilih minimal satu ahli waris yang masih hidup."}

    hasil_ahli = []
    furudh = {}

    # 1. Pasangan
    if suami > 0:
        porsi = Fraction(1, 4) if ada_anak else Fraction(1, 2)
        furudh['suami'] = porsi
        hasil_ahli.append({
            "key": "suami", "nama": "Suami", "jumlah": 1,
            "status": "Ashabul Furudh",
            "dalil": "QS. An-Nisa: 12 (1/4 bila ada keturunan, 1/2 bila tanpa keturunan)",
            "porsi_syari": f"{porsi.numerator}/{porsi.denominator}",
            "porsi_frac": porsi
        })
    elif istri > 0:
        porsi = Fraction(1, 8) if ada_anak else Fraction(1, 4)
        furudh['istri'] = porsi
        hasil_ahli.append({
            "key": "istri", "nama": f"Istri ({istri} orang)" if istri > 1 else "Istri", "jumlah": istri,
            "status": "Ashabul Furudh",
            "dalil": "QS. An-Nisa: 12 (1/8 bila ada keturunan, 1/4 bila tanpa keturunan)",
            "porsi_syari": f"{porsi.numerator}/{porsi.denominator} (dibagi rata)" if istri > 1 else f"{porsi.numerator}/{porsi.denominator}",
            "porsi_frac": porsi
        })

    # 2. Ibu
    if ibu > 0:
        if ada_anak or jml_saudara >= 2:
            porsi = Fraction(1, 6)
            ket = "1/6 (karena ada anak/dua saudara atau lebih)"
        else:
            if ayah > 0 and (suami > 0 or istri > 0) and not ada_anak and jml_saudara == 0:
                porsi_pasangan = furudh.get('suami') or furudh.get('istri')
                sisa_pasangan = Fraction(1, 1) - porsi_pasangan
                porsi = sisa_pasangan * Fraction(1, 3)
                ket = "1/3 dari sisa setelah pasangan (Gharrawain)"
            else:
                porsi = Fraction(1, 3)
                ket = "1/3 (karena tidak ada keturunan & saudara < 2)"
        furudh['ibu'] = porsi
        hasil_ahli.append({
            "key": "ibu", "nama": "Ibu", "jumlah": 1,
            "status": "Ashabul Furudh",
            "dalil": f"QS. An-Nisa: 11 ({ket})",
            "porsi_syari": f"{porsi.numerator}/{porsi.denominator}",
            "porsi_frac": porsi
        })

    # 3. Ayah
    ayah_tipe = None
    if ayah > 0:
        if anak_laki > 0:
            porsi = Fraction(1, 6)
            furudh['ayah'] = porsi
            ayah_tipe = 'fardh'
            hasil_ahli.append({
                "key": "ayah", "nama": "Ayah", "jumlah": 1,
                "status": "Ashabul Furudh",
                "dalil": "QS. An-Nisa: 11 (1/6 karena ada anak laki-laki)",
                "porsi_syari": "1/6",
                "porsi_frac": porsi
            })
        elif anak_puan > 0:
            porsi = Fraction(1, 6)
            furudh['ayah'] = porsi
            ayah_tipe = 'fardh_ashabah'
            hasil_ahli.append({
                "key": "ayah", "nama": "Ayah", "jumlah": 1,
                "status": "Ashabul Furudh + Ashabah",
                "dalil": "QS. An-Nisa: 11 (Menerima 1/6 fardh dan menghabiskan sisa harta)",
                "porsi_syari": "1/6 + Sisa",
                "porsi_frac": porsi
            })
        else:
            ayah_tipe = 'ashabah'
            hasil_ahli.append({
                "key": "ayah", "nama": "Ayah", "jumlah": 1,
                "status": "Ashabah bin Nafs",
                "dalil": "HR. Bukhari-Muslim (Ahli waris laki-laki terdekat menghabiskan sisa)",
                "porsi_syari": "Sisa Harta (Ashabah)",
                "porsi_frac": Fraction(0, 1)
            })

    # 4. Anak Perempuan (jika tanpa anak laki-laki)
    if anak_puan > 0 and anak_laki == 0:
        porsi = Fraction(1, 2) if anak_puan == 1 else Fraction(2, 3)
        furudh['anak_perempuan'] = porsi
        hasil_ahli.append({
            "key": "anak_perempuan",
            "nama": f"Anak Perempuan ({anak_puan} orang)" if anak_puan > 1 else "Anak Perempuan",
            "jumlah": anak_puan,
            "status": "Ashabul Furudh",
            "dalil": "QS. An-Nisa: 11 (1/2 jika tunggal, 2/3 jika 2 orang atau lebih)",
            "porsi_syari": f"{porsi.numerator}/{porsi.denominator} (dibagi rata)" if anak_puan > 1 else f"{porsi.numerator}/{porsi.denominator}",
            "porsi_frac": porsi
        })

    # 5. Saudara (Cek Hijab Hirman)
    if saudara_laki > 0 or saudara_puan > 0:
        if ayah > 0 or anak_laki > 0:
            if saudara_laki > 0:
                hasil_ahli.append({
                    "key": "saudara_laki", "nama": f"Saudara Laki-laki ({saudara_laki} orang)", "jumlah": saudara_laki,
                    "status": "Mahjub (Terhalang)", "dalil": "Terhalang (Mahjub Hirman) karena adanya Ayah / Anak Laki-laki",
                    "porsi_syari": "0 (Mahjub)", "porsi_frac": Fraction(0, 1), "nominal_total": 0.0, "nominal_per_orang": 0.0
                })
            if saudara_puan > 0:
                hasil_ahli.append({
                    "key": "saudara_perempuan", "nama": f"Saudara Perempuan ({saudara_puan} orang)", "jumlah": saudara_puan,
                    "status": "Mahjub (Terhalang)", "dalil": "Terhalang (Mahjub Hirman) karena adanya Ayah / Anak Laki-laki",
                    "porsi_syari": "0 (Mahjub)", "porsi_frac": Fraction(0, 1), "nominal_total": 0.0, "nominal_per_orang": 0.0
                })

    total_fardh = sum(furudh.values())
    sisa_frac = Fraction(1, 1) - total_fardh
    catatan_kaidah = ""

    # Sisa Ashabah ke Anak
    if anak_laki > 0:
        total_saham = (anak_laki * 2) + (anak_puan * 1)
        sisa_rp = max(0.0, float(sisa_frac) * tirkah_bersih)
        nilai_per_saham = sisa_rp / total_saham if total_saham > 0 else 0.0

        total_anak_laki_rp = nilai_per_saham * (anak_laki * 2)
        hasil_ahli.append({
            "key": "anak_laki",
            "nama": f"Anak Laki-laki ({anak_laki} orang)" if anak_laki > 1 else "Anak Laki-laki",
            "jumlah": anak_laki,
            "status": "Ashabah bin Nafs" if anak_puan == 0 else "Ashabah bil Ghair (Rasio 2 : 1)",
            "dalil": "QS. An-Nisa: 11 (Bagian 1 anak laki-laki setara bagian 2 anak perempuan)",
            "porsi_syari": "Ashabah (Sisa)",
            "porsi_frac": Fraction(anak_laki * 2, total_saham) * sisa_frac if sisa_frac > 0 else Fraction(0, 1),
            "nominal_total": total_anak_laki_rp,
            "nominal_per_orang": total_anak_laki_rp / anak_laki if anak_laki > 0 else 0
        })

        if anak_puan > 0:
            total_anak_puan_rp = nilai_per_saham * (anak_puan * 1)
            hasil_ahli.append({
                "key": "anak_perempuan",
                "nama": f"Anak Perempuan ({anak_puan} orang)" if anak_puan > 1 else "Anak Perempuan",
                "jumlah": anak_puan,
                "status": "Ashabah bil Ghair (Rasio 2 : 1)",
                "dalil": "QS. An-Nisa: 11 (Bagian anak perempuan 1 saham berbanding 2)",
                "porsi_syari": "Ashabah (Sisa)",
                "porsi_frac": Fraction(anak_puan * 1, total_saham) * sisa_frac if sisa_frac > 0 else Fraction(0, 1),
                "nominal_total": total_anak_puan_rp,
                "nominal_per_orang": total_anak_puan_rp / anak_puan if anak_puan > 0 else 0
            })
    elif ayah_tipe in ['ashabah', 'fardh_ashabah'] and sisa_frac > 0:
        sisa_rp = float(sisa_frac) * tirkah_bersih
        for h in hasil_ahli:
            if h['key'] == 'ayah':
                if ayah_tipe == 'ashabah':
                    h['nominal_total'] = sisa_rp
                    h['nominal_per_orang'] = sisa_rp
                    h['porsi_frac'] = sisa_frac
                else:
                    fardh_rp = float(h['porsi_frac']) * tirkah_bersih
                    h['nominal_total'] = fardh_rp + sisa_rp
                    h['nominal_per_orang'] = h['nominal_total']
                    h['porsi_frac'] = h['porsi_frac'] + sisa_frac

    # Kaidah 'Aul & Radd
    if not anak_laki and ayah_tipe != 'ashabah':
        if total_fardh > Fraction(1, 1):
            catatan_kaidah = f"Kaidah 'Aul diterapkan: Total furudh ({total_fardh.numerator}/{total_fardh.denominator}) melebihi 1, bagian disesuaikan secara proporsional."
            for h in hasil_ahli:
                if h.get('porsi_frac', Fraction(0, 1)) > 0:
                    p_baru = h['porsi_frac'] / total_fardh
                    h['porsi_frac'] = p_baru
                    h['porsi_syari'] += f" ('Aul: {p_baru.numerator}/{p_baru.denominator})"
                    h['nominal_total'] = float(p_baru) * tirkah_bersih
                    h['nominal_per_orang'] = h['nominal_total'] / h['jumlah']
        elif total_fardh < Fraction(1, 1) and sisa_frac > 0 and ayah_tipe != 'fardh_ashabah':
            ahli_radd = [h for h in hasil_ahli if h['key'] not in ['suami', 'istri'] and h.get('porsi_frac', Fraction(0, 1)) > 0]
            if ahli_radd:
                total_radd_frac = sum(h['porsi_frac'] for h in ahli_radd)
                catatan_kaidah = f"Kaidah Radd diterapkan: Sisa harta ({sisa_frac.numerator}/{sisa_frac.denominator}) dikembalikan proporsional kepada ahli waris nasabiah."
                sisa_rp = float(sisa_frac) * tirkah_bersih
                for h in ahli_radd:
                    rasio = h['porsi_frac'] / total_radd_frac
                    tambah_rp = float(rasio) * sisa_rp
                    h['nominal_total'] = (float(h['porsi_frac']) * tirkah_bersih) + tambah_rp
                    h['nominal_per_orang'] = h['nominal_total'] / h['jumlah']
                    h['porsi_syari'] += " (+ Radd)"

    for h in hasil_ahli:
        if 'nominal_total' not in h:
            nom_tot = float(h.get('porsi_frac', Fraction(0, 1))) * tirkah_bersih
            h['nominal_total'] = nom_tot
            h['nominal_per_orang'] = nom_tot / h['jumlah']

    for h in hasil_ahli:
        h['porsi_desimal'] = round(float(h.get('porsi_frac', 0)), 4)
        h['persen'] = f"{h['porsi_desimal'] * 100:.2f}%"
        h.pop('porsi_frac', None)

    total_terbagi = sum(h['nominal_total'] for h in hasil_ahli)

    return {
        "status": "ok",
        "ringkasan": {
            "tirkah_kotor": tirkah_kotor,
            "kewajiban": kewajiban,
            "wasiat": wasiat_disetujui,
            "catatan_wasiat": catatan_wasiat,
            "tirkah_bersih": tirkah_bersih,
            "total_fardh": f"{total_fardh.numerator}/{total_fardh.denominator}",
            "catatan_kaidah": catatan_kaidah
        },
        "daftar_ahli": hasil_ahli,
        "total_terbagi": round(total_terbagi, 2)
    }


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


@app.route("/admin/ganti-password", methods=["POST"])
@admin_required
def ganti_password():
    data    = request.json or {}
    pw_lama = data.get("pw_lama", "")
    pw_baru = data.get("pw_baru", "")
    if not pw_lama or not pw_baru:
        return jsonify({"error": "Password lama dan baru wajib diisi."}), 400
    if len(pw_baru) < 6:
        return jsonify({"error": "Password baru minimal 6 karakter."}), 400
    user = AdminUser.query.filter_by(username=session.get("admin_username")).first()
    if not user:
        return jsonify({"error": "Akun tidak ditemukan."}), 404
    if not check_password_hash(user.password, pw_lama):
        return jsonify({"error": "Password lama salah."}), 403
    user.password = generate_password_hash(pw_baru)
    db.session.commit()
    return jsonify({"status": "ok"})


@app.route("/hapus/<int:pid>", methods=["POST"])
@admin_required
def hapus_data(pid):
    p = db.session.get(Percakapan, pid)
    if not p:
        return jsonify({"error": "Data tidak ditemukan."}), 404
    db.session.delete(p)
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