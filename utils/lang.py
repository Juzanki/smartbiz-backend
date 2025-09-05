# backend/utils/lang.py

translations = {
    "en": {
        "welcome": "Welcome to SmartBiz",
        "ai_help": "How can I assist your business today?"
    },
    "sw": {
        "welcome": "Karibu SmartBiz",
        "ai_help": "Ninawezaje kusaidia biashara yako leo?"
    },
    "fr": {
        "welcome": "Bienvenue sur SmartBiz",
        "ai_help": "Comment puis-je vous aider aujourd'hui?"
    },
    "rw": {
        "welcome": "Ikaze kuri SmartBiz",
        "ai_help": "Nshobora gute kugufasha mu bucuruzi bwawe uyu munsi?"
    },
    "ha": {
        "welcome": "Barka da zuwa SmartBiz",
        "ai_help": "Ta yaya zan iya taimaka wa kasuwancinku yau?"
    },
    "sh": {
        "welcome": "Tikugashire mu SmartBiz",
        "ai_help": "Ndingaitei kukubatsira mubhizinesi rako nhasi?"
    }
}

def translate(key: str, lang: str = "en") -> str:
    """
    Retrieve a translated string by key and language.
    Falls back to English if not found.
    """
    return translations.get(lang, translations["en"]).get(key, key)
