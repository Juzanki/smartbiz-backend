from openai import OpenAI
import json
import os

# 🔐 Load environment variables
api_key = os.getenv("OPENAI_API_KEY")

# ✅ Thibitisha API key ipo
if not api_key:
    raise Exception(
        "🚫 OPENAI_API_KEY haijapatikana kwenye .env file. Tafadhali ongeza key yako.")

# 🧠 Tengeneza OpenAI client
client = OpenAI(api_key=api_key)

# 📂 Tafuta faili linalolengwa na prompt


def get_target_file(prompt):
    with open("file_map.json", "r") as f:
        mapping = json.load(f)
    for keyword, filepath in mapping.items():
        if keyword.lower() in prompt.lower():
            return filepath
    return "output.txt"  # fallback if keyword haijagunduliwa

# ✍️ Tuma prompt kwa OpenAI na andika response


def write_code_to_file(prompt):
    filepath = get_target_file(prompt)
    print(f"⏳ Inatuma ombi kwa ChatGPT kwa prompt: {prompt}")

    try:
        # ⚙️ Tumia gpt-4, fallback gpt-3.5-turbo kama huna access
        try:
            response = client.chat.completions.create(
                model="gpt-4",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5
            )
        except Exception as e:
            print("⚠️ GPT-4 haipatikani. Inatumia gpt-3.5-turbo badala yake...")
            response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5
            )

        content = response.choices[0].message.content.strip()

        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "a", encoding="utf-8") as f:
            f.write("\n\n# ======= GPT RESPONSE =======\n")
            f.write(content)

        print(f"✅ Code imeongezwa ndani ya: {filepath}")

    except Exception as error:
        print(f"❌ Hitilafu imetokea: {error}")


# ▶️ Main Execution
if __name__ == "__main__":
    if not os.path.exists("prompt.txt"):
        print("⚠️ prompt.txt haipo. Tafadhali tengeneza na uandike maombi yako.")
    else:
        with open("prompt.txt", "r", encoding="utf-8") as p:
            prompt = p.read().strip()
        if prompt:
            write_code_to_file(prompt)
        else:
            print("⚠️ Hakuna maandishi kwenye prompt.txt.")
