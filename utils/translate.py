from deep_translator import GoogleTranslator

def translate_text(text: str, target_lang: str = "sw") -> str:
    return GoogleTranslator(source="auto", target=target_lang).translate(text)
