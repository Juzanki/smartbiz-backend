import os
import whisper

MODEL = None

def load_model():
    global MODEL
    if MODEL is None:
        MODEL = whisper.load_model("base")
    return MODEL

def generate_caption_from_video(video_path: str) -> str:
    model = load_model()
    try:
        result = model.transcribe(video_path)
        return result["text"]
    except Exception as e:
        print(f"Error in captioning: {e}")
        return ""
