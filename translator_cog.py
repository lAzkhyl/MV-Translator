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
    groq_client = groq.AsyncGroq(api_key=GROQ_API_KEY)
    MODEL_GROQ = "llama-3.1-8b-instant" 
    print("Translator Cog: API Key Groq berhasil dimuat.")
else:
    groq_client = None
    print("Translator Cog: WARNING!!! GROQ_API_KEY tidak ditemukan.")

# --- 3. HELPER FUNGSI ARSITEKTUR (Berdasarkan Intel) ---

def build_system_prompt():
    """
    Membangun System Prompt dengan Mini-Dictionary (Few-Shot/CoD).
    Diperkuat untuk memaksa output JSON.
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
    
    # Template ini diperkuat untuk mode json_object
    prompt_template = """Anda adalah layanan terjemahan API JSON yang sangat akurat.
Tugas Anda adalah menerjemahkan array JSON dari chat Indonesia (slang) ke {target_lang}.

PERATURAN UTAMA:
1. Terjemahkan HANYA nilai "t" (teks) untuk setiap objek.
2. JANGAN PERNAH meringkas. Terjemahkan 1-ke-1.
3. Kembalikan HANYA JSON yang valid. JANGAN tambahkan teks pembuka atau penutup.

KAMUS SLANG (REFERENSI KUNCI):
{dictionary_json}

Input akan berupa array JSON: `[{"id": "...", "u": "...", "t": "..."}, ...]`
Output Anda HARUS berupa array JSON dengan format yang sama persis: `[{"id": "...", "tl": "..."}, ...]`
"""
    
    return prompt_template.format(
        target_lang=TARGET_LANG,
        dictionary_json=json.dumps(mini_dictionary)
    )

def build_translation_payload(message_batch: list) -> str:
    """
    Mengonversi batch pesan dari Discord menjadi payload JSON minified.
    """
    payload = []
    for msg in message_batch:
        payload.append({
            "id": str(msg.id), # Gunakan ID pesan untuk pemetaan
            "u": msg.author.display_name, # 'u' = user
            "t": msg.content # 't' = text
        })
    # Menggunakan minified JSON (tanpa spasi)
    return json.dumps(payload, separators=(',', ':'))

# --- SKEMA LAMA DIHAPUS ---
# Fungsi define_output_schema() dan variabel OUTPUT_SCHEMA dihapus
# karena Groq tidak mendukung "json_schema" untuk model ini.

# Simpan System Prompt saat startup
SYSTEM_PROMPT = build_system_prompt()


# --- 4. KELAS COG ---
class TranslatorCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.startup_time = datetime.datetime.now(datetime.timezone.utc)
        self.message_batch = [] 
        self.batch_task = None 
        print(f"Translator Cog: Loaded. Monitoring Channel ID {SOURCE_CHANNEL_ID}.")
        print(f"Translator Cog: Startup time set to {self.startup_time}")

    # --- 5. LISTENER ON_MESSAGE (Jaring Pengumpul) ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):

        # Filter (Tetap sama)
        if message.created_at < self.startup_time: return
        if message.author.bot: return
        if message.channel.id != SOURCE_CHANNEL_ID: return
        if message.channel.id == TARGET_THREAD_ID: return
        if message.channel.type == discord.ChannelType.public_thread and message.channel.id == TARGET_THREAD_ID:
             return

        # Lolos Filter: Masukkan ke antrean batch
        self.message_batch.append(message)

        if self.batch_task is None:
            self.batch_task = asyncio.create_task(self.process_batch())

    # --- 6. PROSESOR BATCH (Logika "Sekalian" / Cooldown) ---
    async def process_batch(self):
        await asyncio.sleep(BATCH_COOLDOWN_SECONDS)

        messages_to_translate = list(self.message_batch)
        self.message_batch.clear()
        self.batch_task = None 

        if not messages_to_translate: return
        if not groq_client:
            print("Batching: Groq client tidak ada, proses dibatalkan.")
            return

        print(f"Batching: Memproses {len(messages_to_translate)} pesan...")

        # --- 7. PERSIAPAN TRANSLATE (Format untuk Groq) ---
        user_payload = build_translation_payload(messages_to_translate)

        # --- 8. PANGGIL API (Groq dengan JSON Object) ---
        try:
            chat_completion = await groq_client.chat.completions.create(
                model=MODEL_GROQ,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_payload}
                ],
                
                # --- PERBAIKAN INTI ---
                # Mengganti 'json_schema' dengan 'json_object' yang didukung
                response_format={
                    "type": "json_object"
                },
                
                temperature=0.0, 
                max_tokens=2048,
                timeout=15.0 
            )
            
            # Output DIJAMIN berupa string JSON yang valid
            raw_output = chat_completion.choices[0].message.content
            translated_batch = json.loads(raw_output) 
            
            # Buat kamus/map untuk pemetaan O(1) yang cepat
            # Kita asumsikan Groq mengikuti prompt kita dan mengembalikan array
            if isinstance(translated_batch, list):
                 translation_map = {item['id']: item['tl'] for item in translated_batch if 'id' in item and 'tl' in item}
            else:
                # Jika Groq mengembalikan dict (bukan list), ini adalah error format
                print("Translate Error: Groq mengembalikan JSON Object, bukan Array.")
                raise Exception("Format output tidak sesuai, Groq tidak mengembalikan array.")

        except Exception as e:
            print(f"Translate Error (Groq atau JSON Format): {e}")
            translation_map = {str(msg.id): "[Translation Failed]" for msg in messages_to_translate}

        # --- 9. FORMATTING OUTPUT (Sesuai Keinginan Anda) ---
        output_lines = []
        last_author_id = None

        for original_message in messages_to_translate:
            if last_author_id is not None and last_author_id != original_message.author.id:
                output_lines.append("") # Jarak vertikal

            translated_text = translation_map.get(str(original_message.id), "[Error: No Translation]")
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

            full_description = "\n".join(output_lines)
            
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