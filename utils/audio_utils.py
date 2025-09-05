from moviepy.editor import VideoFileClip
import speech_recognition as sr
import os
from typing import Optional
import logging

# Setup logging (optional for debugging)
logging.basicConfig(level=logging.INFO)


def extract_audio_from_video(video_path: str, output_format: str = "mp3") -> str:
    """
    Extract audio from a video file and save it as an audio file.
    
    Args:
        video_path (str): Path to the input video file.
        output_format (str): Format of the output audio file (e.g., 'mp3', 'wav').

    Returns:
        str: Path to the saved audio file.
    """
    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"Video not found: {video_path}")

    try:
        logging.info(f"🔊 Extracting audio from: {video_path}")
        clip = VideoFileClip(video_path)
        audio = clip.audio

        audio_filename = os.path.splitext(video_path)[0] + f".{output_format}"
        audio.write_audiofile(audio_filename)
        logging.info(f"✅ Audio saved to: {audio_filename}")

        return audio_filename
    except Exception as e:
        logging.error(f"❌ Error extracting audio: {e}")
        raise e
    finally:
        if 'clip' in locals():
            clip.close()


def transcribe_audio(audio_path: str, lang: str = "sw") -> str:
    """
    Transcribe spoken audio into text using Google's Speech Recognition API.

    Args:
        audio_path (str): Path to the audio file (.wav, .mp3, etc).
        lang (str): Language code ('sw' for Swahili, 'en' for English).

    Returns:
        str: Transcribed text.
    """
    if not os.path.isfile(audio_path):
        raise FileNotFoundError(f"Audio not found: {audio_path}")

    recognizer = sr.Recognizer()

    try:
        with sr.AudioFile(audio_path) as source:
            logging.info(f"🎧 Transcribing audio: {audio_path}")
            audio = recognizer.record(source)

        text = recognizer.recognize_google(audio, language=lang)
        logging.info("✅ Transcription successful")
        return text

    except sr.UnknownValueError:
        logging.warning("🤷 Could not understand audio")
        return "Could not understand audio"

    except sr.RequestError as e:
        logging.error(f"🛑 API Error: {e}")
        return "Speech recognition service unavailable"

    except Exception as e:
        logging.error(f"⚠️ Transcription failed: {e}")
        return "Unexpected error during transcription"


def generate_voice_response(text: str, output_path: str = "response.mp3") -> str:
    """
    Convert text into speech and save as an MP3 file.
    
    Args:
        text (str): The text to convert.
        output_path (str): Destination file for the audio.

    Returns:
        str: Path to the generated audio file.
    """
    tts = gTTS(text=text, lang="sw")  # tumia 'en' kwa Kiingereza
    tts.save(output_path)
    return output_path
