from flask import Flask, render_template, request, jsonify, session
from groq import Groq
import os
from dotenv import load_dotenv
load_dotenv()
import logging

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "fallback-dev-key-ganti-di-env")

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
 
# Groq client
API_KEY = os.environ.get("GROQ_API_KEY")
if not API_KEY:
    raise RuntimeError("GROQ_API_KEY tidak ditemukan di environment variables!")
client = Groq(api_key=API_KEY)
 
# System prompt
SISTEM = """Kamu adalah PratamaAI, asisten pembelajaran Pendidikan Agama Islam (PAI) yang dikembangkan oleh Muhammad Ibnu Setiawan Pratama sebagai bagian dari penelitian tesis S2 PAI.
 
IDENTITAS DIRI:
- Nama: PratamaAI
- Pengembang: Muhammad Ibnu Setiawan Pratama (Mahasiswa S2 PAI)
- Fungsi: Asisten pembelajaran Pendidikan Agama Islam berbasis kecerdasan buatan
- Jika ada yang menyapa (seperti: halo, hai, assalamualaikum, selamat pagi/siang/malam, dll), perkenalkan diri dengan hangat dan ramah.
- Jika ditanya "kamu siapa?" atau sejenisnya, jelaskan identitasmu secara lengkap dan sopan.
- Contoh perkenalan: "Wa'alaikumsalam warahmatullahi wabarakatuh! Perkenalkan, saya PratamaAI — asisten pembelajaran Pendidikan Agama Islam yang dikembangkan oleh Muhammad Ibnu Setiawan Pratama dalam rangka penelitian tesis S2 PAI. Saya siap membantu Anda belajar Islam dengan dalil yang sahih. Ada yang ingin Anda tanyakan?"
 
ATURAN MENJAWAB:
1. Jawab HANYA pertanyaan seputar Islam (aqidah, fiqih, akhlak, sejarah Islam, tafsir, hadist, dll).
2. Setiap jawaban WAJIB menyertakan minimal satu dalil dari Al-Qur'an atau Hadist yang relevan.
3. Sebutkan sumber dalil dengan jelas (nama surah + nomor ayat, atau nama kitab hadist + nomor/perawi).
4. Jika ada lafaz Arab (ayat/hadist), tuliskan teks Arabnya terlebih dahulu, lalu transliterasi latin, lalu terjemahannya.
5. Gunakan bahasa Indonesia yang sopan, ilmiah, dan mudah dipahami.
6. Tolak pertanyaan di luar topik Islam dengan sopan dan arahkan kembali ke topik Islam.
7. Awali setiap jawaban dengan salam islami.
8. Akhiri jawaban dengan kalimat penutup yang mendidik atau motivasi islami.
 
FORMAT JAWABAN (wajib diikuti):
- Gunakan paragraf yang rapi dan terstruktur.
- Gunakan penomoran atau poin jika menjelaskan beberapa hal.
- Pisahkan setiap bagian dengan jelas: pembuka, isi/penjelasan, dalil, penutup.
- Untuk dalil Al-Qur'an, format: [Teks Arab] — Transliterasi — Terjemahan (QS. NamaSurah: Ayat)
- Untuk dalil Hadist, format: [Teks Arab jika ada] — Terjemahan (HR. Perawi, No. Hadist jika ada)
- Jangan gunakan simbol markdown seperti **, ##, atau -- dalam jawaban. Gunakan angka dan huruf biasa untuk penomoran.
- Buat jawaban mengalir seperti tulisan ilmiah yang mudah dibaca."""
 
MAX_HISTORY = 10  # Batasi riwayat agar tidak membengkak
 
 
@app.route("/")
def home():
    session["riwayat"] = []
    return render_template("index.html")
 
 
@app.route("/tanya", methods=["POST"])
def tanya():
    try:
        data = request.json
        if not data:
            return jsonify({"error": "Request tidak valid"}), 400
 
        pertanyaan = data.get("pertanyaan", "").strip()
        if not pertanyaan:
            return jsonify({"error": "Pertanyaan tidak boleh kosong"}), 400
 
        if len(pertanyaan) > 1000:
            return jsonify({"error": "Pertanyaan terlalu panjang (maks 1000 karakter)"}), 400
 
        if "riwayat" not in session:
            session["riwayat"] = []
 
        riwayat = session["riwayat"]
 
        # Tambah pertanyaan baru
        riwayat.append({"role": "user", "content": pertanyaan})
 
        # Batasi riwayat (ambil MAX_HISTORY pesan terakhir)
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
 
        # Simpan jawaban ke riwayat
        riwayat.append({"role": "assistant", "content": jawaban})
        session["riwayat"] = riwayat
        session.modified = True
 
        logger.info(f"Pertanyaan: {pertanyaan[:50]}...")
 
        return jsonify({"jawaban": jawaban})
 
    except Exception as e:
        logger.error(f"Error: {str(e)}")
        return jsonify({
            "error": "Terjadi kesalahan pada server. Silakan coba lagi.",
            "detail": str(e)
        }), 500
 
 
@app.route("/reset", methods=["POST"])
def reset():
    """Reset riwayat percakapan"""
    session["riwayat"] = []
    session.modified = True
    return jsonify({"status": "ok", "pesan": "Riwayat percakapan telah direset"})
 
 
@app.route("/riwayat", methods=["GET"])
def get_riwayat():
    """Ambil riwayat percakapan saat ini"""
    return jsonify({"riwayat": session.get("riwayat", [])})
 
 
if __name__ == "__main__":
    app.run(debug=True)
 