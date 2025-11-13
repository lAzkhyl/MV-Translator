import discord
from discord.ext import commands
import asyncio
import datetime
import os
import groq
import json

# --- 1. KONFIGURASI (GANTI INI) ---

# ID Channel Indonesia (Tempat user mengetik)
SOURCE_CHANNEL_ID = 1433114079249039431

# ID Thread (Tempat bot mengirim terjemahan)
TARGET_THREAD_ID = 1438150645080260740

# Bahasa Target (untuk API)
TARGET_LANG = "English" 

# Cooldown: Seberapa lama bot mengumpulkan pesan (dalam detik)
BATCH_COOLDOWN_SECONDS = 10 

# --- 2. KONFIGURASI GROQ ---
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

if GROQ_API_KEY:
    # Kita menggunakan client Groq Asinkron untuk discord.py
    groq_client = groq.AsyncGroq(api_key=GROQ_API_KEY)
    MODEL_GROQ = "llama-3.1-8b-instant" # Model yang mendukung JSON Schema [cite: 393, 503]
    print("Translator Cog: API Key Groq berhasil dimuat.")
else:
    groq_client = None
    print("Translator Cog: WARNING!!! GROQ_API_KEY tidak ditemukan.")

# --- 3. HELPER FUNGSI ARSITEKTUR (Berdasarkan Intel) ---

def build_system_prompt():
    """
    Membangun System Prompt dengan Mini-Dictionary (Few-Shot/CoD).
    [cite_start]Ini mengkondisikan model untuk menangani slang [cite: 425, 431, 510-514].
    """
    mini_dictionary = {
        "yg": "that/which/who",
        "gw": "I/me",
        "lu": "you",
        "bnr": "serious/really",
        "bgt": "very/super",
        "mager": "lazy/unmotivated",
        "otw": "on my way",
        "gpp": "it's okay/no problem",
        "lg": "again / currently (doing)",
        "dgn": "with",
        "ga": "no/not",
        "utk": "for",
        "bs": "can",
        "jgn": "don't",
        "sm": "with/and"
    }
    
    # Format Llama 3.1 Instruct
    prompt = f"""<|start_header_id|>system<|end_header_id|>
Anda adalah layanan terjemahan API yang sangat efisien dan akurat.
Tugas Anda adalah menerjemahkan batch pesan obrolan dari bahasa Indonesia informal (termasuk bahasa gaul berat, typo, dan campuran) ke {TARGET_LANG} yang natural dan akurat.

PERATURAN UTAMA:
1. Terjemahkan HANYA nilai "t" (teks) untuk setiap objek dalam array JSON yang diberikan.
2. Jangan pernah meringkas. Terjemahkan pesan 1-ke-1.
3. Pertahankan makna dan nada asli (kasual, mendesak, dll.).
4. Kembalikan HANYA array JSON yang divalidasi skema.

KAMUS SLANG (GUNAKAN SEBAGAI REFERENSI KUNCI):
{json.dumps(mini_dictionary)}

Input akan berupa array JSON dari objek `{{"id": "...", "u": "...", "t": "..."}}`.
Output Anda HARUS berupa array JSON yang divalidasi skema dari objek `{{"id": "...", "tl": "..."}}`.<|eot_id|>
"""
    return prompt

def build_translation_payload(message_batch: list) -> str:
    """
    Mengonversi batch pesan dari Discord menjadi payload JSON minified[cite: 518].
    Ini sangat efisien token[cite: 481].
    """
    payload = []
    for msg in message_batch:
        payload.append({
            "id": str(msg.id), # Gunakan ID pesan untuk pemetaan [cite: 466]
            "u": msg.author.display_name, # 'u' = user
            "t": msg.content # 't' = text
        })
    # Menggunakan minified JSON (tanpa spasi) [cite: 519]
    return json.dumps(payload, separators=(',', ':'))

def define_output_schema() -> dict:
    """
    Mendefinisikan 'hard rail' JSON Schema[cite: 520].
    Ini memaksa Groq untuk mengembalikan data dalam format ini[cite: 394, 501].
    """
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "ID pesan asli untuk pemetaan kembali"
                },
                "tl": {
                    "type": "string",
                    "description": f"Teks terjemahan {TARGET_LANG}"
                }
            },
            "required": ["id", "tl"] # Memaksa ID untuk pemetaan [cite: 523]
        }
    }

# Simpan System Prompt & Skema saat startup (efisien)
SYSTEM_PROMPT = build_system_prompt()
OUTPUT_SCHEMA = define_output_schema()


# --- 4. KELAS COG ---
class TranslatorCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.startup_time = datetime.datetime.now(datetime.timezone.utc)
        self.message_batch = [] # Antrean "sekalian"
        self.batch_task = None # Penanda tugas
        print(f"Translator Cog: Loaded. Monitoring Channel ID {SOURCE_CHANNEL_ID}.")
        print(f"Translator Cog: Startup time set to {self.startup_time}")

    # --- 5. LISTENER ON_MESSAGE (Jaring Pengumpul) ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):

        # Filter 1: Abaikan pesan lama saat startup
        if message.created_at < self.startup_time:
            return

        # Filter 2: Abaikan bot (Pencegah Loop Fatal)
        if message.author.bot:
            return

        # Filter 3: Hanya channel Indo
        if message.channel.id != SOURCE_CHANNEL_ID:
            return
            
        # Filter 4: Abaikan thread terjemahan (Pencegah Loop Fatal)
        if message.channel.id == TARGET_THREAD_ID:
             return
        if message.channel.type == discord.ChannelType.public_thread and message.channel.id == TARGET_THREAD_ID:
             return

        # Lolos Filter: Masukkan ke antrean batch
        self.message_batch.append(message)

        # Jika tugas batching belum berjalan, mulai
        if self.batch_task is None:
            self.batch_task = asyncio.create_task(self.process_batch())

    # --- 6. PROSESOR BATCH (Logika "Sekalian" / Cooldown) ---
    async def process_batch(self):
        # Tunggu X detik untuk mengumpulkan pesan lain
        await asyncio.sleep(BATCH_COOLDOWN_SECONDS)

        messages_to_translate = list(self.message_batch)
        self.message_batch.clear()
        self.batch_task = None # Siap untuk batch berikutnya

        if not messages_to_translate:
            return

        if not groq_client:
            print("Batching: Groq client tidak ada, proses dibatalkan.")
            return

        print(f"Batching: Memproses {len(messages_to_translate)} pesan...")

        # --- 7. PERSIAPAN TRANSLATE (Format untuk Groq) ---
        
        # Buat payload JSON minified
        user_payload = build_translation_payload(messages_to_translate)

        # --- 8. PANGGIL API (Groq dengan JSON Schema) ---
        try:
            chat_completion = await groq_client.chat.completions.create(
                model=MODEL_GROQ,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_payload}
                ],
                # 'Hard Rail' arsitektural [cite: 528]
                response_format={
                    "type": "json_schema",
                    "json_schema": OUTPUT_SCHEMA
                },
                temperature=0.0, # Penting untuk konsistensi terjemahan [cite: 528]
                max_tokens=2048,
                timeout=15.0 # Timeout 15 detik [cite: 529]
            )
            
            # Output DIJAMIN berupa string JSON yang valid [cite: 529]
            raw_output = chat_completion.choices[0].message.content
            translated_batch = json.loads(raw_output) # Konversi ke dict Python [cite: 529]
            
            # Buat kamus/map untuk pemetaan O(1) yang cepat [cite: 533]
            translation_map = {item['id']: item['tl'] for item in translated_batch}

        except Exception as e:
            print(f"Translate Error (Groq): {e}")
            # Buat map palsu agar bot bisa melaporkan error per pesan
            translation_map = {str(msg.id): "[Translation Failed]" for msg in messages_to_translate}


        # --- 9. FORMATTING OUTPUT (Sesuai Keinginan Anda) ---
        output_lines = []
        last_author_id = None

        for original_message in messages_to_translate:
            
            # Tambahkan jarak vertikal jika pembicara berganti
            if last_author_id is not None and last_author_id != original_message.author.id:
                output_lines.append("") # Jarak vertikal

            translated_text = translation_map.get(str(original_message.id), "[Error: No Translation]")
            
            # Format: **Nama:** Teks
            output_lines.append(f"**{original_message.author.display_name}:** {translated_text}")
            last_author_id = original_message.author.id

        # --- 10. KIRIM KE THREAD ---
        try:
            source_channel = self.bot.get_channel(SOURCE_CHANNEL_ID)
            if not source_channel:
                print("Error: Tidak dapat menemukan SOURCE_CHANNEL_ID")
                return

            target_thread = source_channel.get_thread(TARGET_THREAD_ID)
            
            if not target_thread:
                print("Warning: Thread tidak ditemukan, membuat ulang...")
                target_thread = await source_channel.create_thread(
                    name=f"{TARGET_LANG.upper()} Translation",
                    type=discord.ChannelType.public_thread
                )

            # Kirim hasil batch ke thread dalam satu embed besar
            # Deskripsi embed punya limit 4096 karakter (aman)
            full_description = "\n".join(output_lines)
            
            # Cegah error jika batch terlalu besar
            if len(full_description) > 4000:
                full_description = full_description[:4000] + "\n... (Message truncated)"

            embed = discord.Embed(
                description=full_description,
                color=discord.Color.blue()
            )
            
            await target_thread.send(embed=embed)
            print(f"Batching: Sukses mengirim {len(messages_to_translate)} terjemahan.")

        except Exception as e:
            print(f"Discord Error: Gagal mengirim ke thread. {e}")


# --- 11. SETUP FUNCTION ---
async def setup(bot):
    await bot.add_cog(TranslatorCog(bot))