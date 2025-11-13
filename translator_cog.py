import discord
from discord.ext import commands
import asyncio
import datetime
import os
import groq
import json
import time

# --- 1. CONFIGURATION (CHANGE THIS) ---

# ID Channel Indonesia (Where users type)
SOURCE_CHANNEL_ID = 1433114079249039431

# ID Thread (Where bot sends translations)
TARGET_THREAD_ID = 1438150645080260740

# Target Language (for API)
TARGET_LANG = "English" 

# Cooldown: How long the bot collects messages (in seconds)
BATCH_COOLDOWN_SECONDS = 10 

# --- 2. GROQ CONFIGURATION ---
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

if GROQ_API_KEY:
    groq_client = groq.AsyncGroq(api_key=GROQ_API_KEY)
    MODEL_GROQ = "llama-3.1-8b-instant" 
    print("Translator Cog: API Key Groq successfully loaded.")
else:
    groq_client = None
    print("Translator Cog: WARNING!!!: GROQ_API_KEY not found.")

# --- 3. ARCHITECTURE HELPER FUNCTIONS ---

def build_system_prompt():
    """
    Builds the System Prompt with a Mini-Dictionary (Few-Shot/CoD).
    Instructs the model to return a JSON Object with a 'translations' key.
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
    
    # This prompt is reinforced for json_object mode
    prompt_template = """You are a JSON translation API service.
Your task is to translate a JSON array of Indonesian chat messages (slang) into {target_lang}.

RULES:
1. Translate ONLY the "t" (text) value for each object.
2. NEVER summarize. Translate 1-to-1.
3. Return ONLY valid JSON. DO NOT add any introductory or closing text.

SLANG DICTIONARY (KEY REFERENCE):
{dictionary_json}

Input will be a JSON array: `[{{\"id\": \"...\", \"u\": \"...\", \"t\": \"...\"}}, ...]`
Your output MUST be a single JSON Object in this exact format:
`{{\"translations\": [{{\"id\": \"...\", \"tl\": \"...\"}}, ...]}}`
"""
    
    return prompt_template.format(
        target_lang=TARGET_LANG,
        dictionary_json=json.dumps(mini_dictionary)
    )

def build_translation_payload(message_batch: list) -> str:
    """
    Converts a batch of Discord messages into a minified JSON payload for the LLM.
    """
    payload = []
    for msg in message_batch:
        payload.append({
            "id": str(msg.id), # Use message ID for mapping
            "u": msg.author.display_name, # 'u' = user
            "t": msg.content # 't' = text
        })
    # Use minified JSON (no spaces) for max token efficiency
    return json.dumps(payload, separators=(',', ':'))

# Load the system prompt globally on startup (efficient)
SYSTEM_PROMPT = build_system_prompt()


# --- 4. COG CLASS ---
class TranslatorCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.startup_time = datetime.datetime.now(datetime.timezone.utc)
        self.message_batch = [] 
        self.batch_task = None 
        print(f"Translator Cog: Loaded. Monitoring Channel ID {SOURCE_CHANNEL_ID}.")
        print(f"Translator Cog: Startup time set to {self.startup_time}")

    # --- 5. ON_MESSAGE LISTENER (The Collector) ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):

        # Filter 1: Ignore old messages on startup
        if message.created_at < self.startup_time: return
        # Filter 2: Ignore bots (prevents fatal loops)
        if message.author.bot: return
        # Filter 3: Only monitor the source channel
        if message.channel.id != SOURCE_CHANNEL_ID: return
        # Filter 4: Ignore the translation thread (prevents fatal loops)
        if message.channel.id == TARGET_THREAD_ID: return
        if message.channel.type == discord.ChannelType.public_thread and message.channel.id == TARGET_THREAD_ID:
             return

        # Passed filters: Add to batch
        self.message_batch.append(message)

        # Start the batch processor if it's not already running
        if self.batch_task is None:
            self.batch_task = asyncio.create_task(self.process_batch())

    # --- 6. BATCH PROCESSOR (The "Sekalian" Logic) ---
    async def process_batch(self):
        # Wait X seconds to gather more messages
        await asyncio.sleep(BATCH_COOLDOWN_SECONDS)

        messages_to_translate = list(self.message_batch)
        self.message_batch.clear()
        self.batch_task = None # Reset for next batch

        if not messages_to_translate: return
        if not groq_client:
            print("Batching: Groq client is not available, canceling process.")
            return

        print(f"Batching: Processing {len(messages_to_translate)} messages...")

        # --- 7. PREPARE TRANSLATION (Format for Groq) ---
        user_payload = build_translation_payload(messages_to_translate)

        # --- 8. CALL API (Groq with json_object mode) ---
        try:
            chat_completion = await groq_client.chat.completions.create(
                model=MODEL_GROQ,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_payload}
                ],
                
                # --- CORE FIX ---
                # Use the supported 'json_object' type
                response_format={
                    "type": "json_object"
                },
                
                temperature=0.0, 
                max_tokens=2048,
                timeout=15.0 
            )
            
            # Output is guaranteed to be a valid JSON string
            raw_output = chat_completion.choices[0].message.content
            json_response = json.loads(raw_output) # This is an Object (dict)
            
            # --- CORE FIX PARSING ---
            # Extract the array from *within* the JSON Object
            if "translations" in json_response and isinstance(json_response.get("translations"), list):
                translated_batch = json_response["translations"] # This is the Array (list)
            else:
                print("Translate Error: Groq returned a valid JSON Object, but not the expected format (missing 'translations' key).")
                raise Exception("Format output did not match, 'translations' key missing.")

            # Create a map for fast O(1) lookup
            translation_map = {item['id']: item['tl'] for item in translated_batch if 'id' in item and 'tl' in item}

        except Exception as e:
            print(f"Translate Error (Groq or JSON Format): {e}")
            translation_map = {str(msg.id): "[Translation Failed]" for msg in messages_to_translate}

        # --- 9. FORMAT OUTPUT (As requested) ---
        output_lines = []
        last_author_id = None

        for original_message in messages_to_translate:
            # Add vertical space if speaker changes
            if last_author_id is not None and last_author_id != original_message.author.id:
                output_lines.append("") 

            translated_text = translation_map.get(str(original_message.id), "[Error: No Translation]")
            output_lines.append(f"**{original_message.author.display_name}:** {translated_text}")
            last_author_id = original_message.author.id

        # --- 10. SEND TO THREAD ---
        try:
            source_channel = self.bot.get_channel(SOURCE_CHANNEL_ID)
            if not source_channel:
                print("Error: Could not find SOURCE_CHANNEL_ID")
                return

            target_thread = source_channel.get_thread(TARGET_THREAD_ID)
            
            if not target_thread:
                print("Warning: Target thread not found, creating new one...")
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
            print(f"Batching: Successfully sent {len(messages_to_translate)} translations.")

        except Exception as e:
            print(f"Discord Error: Failed to send to thread. {e}")


# --- 11. SETUP FUNCTION ---
async def setup(bot):
    await bot.add_cog(TranslatorCog(bot))