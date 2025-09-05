import re

COMMON_WORDS = {
    "the", "a", "an", "is", "are", "to", "of", "in", "on", "for", "and", "with", "you", "this", "that", "it", "we", "our"
}

def generate_hashtags_from_caption(caption: str, max_tags: int = 5) -> str:
    if not caption:
        return ""

    words = re.findall(r"\b\w+\b", caption.lower())
    keywords = [word for word in words if word not in COMMON_WORDS and len(word) > 2]

    unique = list(dict.fromkeys(keywords))  # remove duplicates
    top_keywords = unique[:max_tags]
    hashtags = [f"#{word}" for word in top_keywords]
    return ",".join(hashtags)
