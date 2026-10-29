import os, sys, re
import torch
import torchaudio
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import numpy as np
from moviepy import *
import moviepy.video.fx as vfx

# Настройки видео
W, H = 1080, 1920
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
MUSIC_IDX_FILE = "music_idx.txt"

# 1. Инициализация нейросети Silero TTS
print("Загрузка модели Silero TTS (это может занять минуту при первом запуске)...")
try:
    # ДОБАВЛЕН ПАРАМЕТР trust_repo=True
    model, _ = torch.hub.load(
        repo_or_dir='snakers4/silero-models', 
        model='silero_tts', 
        language='ru', 
        speaker='v5_ru',
        trust_repo=True 
    )
    model.to('cpu')
    print("Модель успешно загружена!")
except Exception as e:
    print("КРИТИЧЕСКАЯ ОШИБКА при загрузке Silero:", e)
    sys.exit(1)

def generate_voice(text, out_file="voice.mp3"):
    print("Генерирую голос...")
    clean_text = re.sub(r'[^\w\s.,!?]', '', text)
    if not clean_text.strip():
        clean_text = "Привет, это тест."
        
    speaker = 'aidar'
    sample_rate = 48000
    
    audio = model.apply_tts(text=clean_text, speaker=speaker, sample_rate=sample_rate)
    
    temp_wav = "voice_temp.wav"
    torchaudio.save(temp_wav, audio.unsqueeze(0), sample_rate)
    
    clip = AudioFileClip(temp_wav)
    clip.write_audiofile(out_file, codec='mp3', logger=None)
    clip.close()
    
    if os.path.exists(temp_wav):
        os.remove(temp_wav)
    print("Голос готов!")

def get_base_bg():
    bg = Image.new('RGBA', (W, H), (10, 5, 20, 255))
    colors = [(130, 0, 255), (0, 100, 255), (255, 0, 150)]
    for _ in range(4):
        c = colors[np.random.randint(0, 3)]
        r = np.random.randint(600, 1000)
        x, y = np.random.randint(-200, W), np.random.randint(-200, H)
        layer = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(layer).ellipse((x, y, x+r, y+r), fill=c + (100,))
        bg = Image.alpha_composite(bg, layer.filter(ImageFilter.GaussianBlur(radius=200)))
    return bg

def create_video(text, title, img_path, brawler_path, out_path="result.mp4"):
    generate_voice(text, "voice.mp3")
    
    print("Готовлю картинки...")
    bg_img = get_base_bg()
    draw = ImageDraw.Draw(bg_img)
    try:
        font_title = ImageFont.truetype(FONT_PATH, 110)
    except:
        font_title = ImageFont.load_default()
        
    draw.text((W/2, 200), title, font=font_title, fill="#ffcc00", anchor="mm", stroke_width=6, stroke_fill="black")
    bg_img.convert("RGB").save("bg_temp.png")
    
    if os.path.exists(img_path):
        meta_img = Image.open(img_path).convert("RGBA")
        meta_img.thumbnail((900, 900), Image.Resampling.LANCZOS)
        meta_img.save("meta_temp.png")

    if os.path.exists(brawler_path):
        brawler_img = Image.open(brawler_path).convert("RGBA")
        brawler_img.thumbnail((450, 450), Image.Resampling.LANCZOS)
        brawler_img.save("brawler_temp.png")

    print("Собираю видео...")
    clip_bg = ImageClip("bg_temp.png").with_duration(15)
    clips = [clip_bg]
    
    if os.path.exists("meta_temp.png"):
        clip_meta = (ImageClip("meta_temp.png")
                     .with_duration(15)
                     .with_position(("center", "center"))
                     .with_effects([vfx.CrossFadeIn(1)])
                     .resized(lambda t: 1 + 0.05*np.sin(t*2)))
        clips.append(clip_meta)
    
    if os.path.exists("brawler_temp.png"):
        clip_brawler = (ImageClip("brawler_temp.png")
                        .with_duration(15)
                        .with_position((W - 400, H - 450))
                        .resized(lambda t: 1 + 0.03*np.sin(t*10)))
        clips.append(clip_brawler)
    
    clip_voice = AudioFileClip("voice.mp3")
    duration = clip_voice.duration + 0.5
    
    for c in clips:
        c.with_duration(duration)
        
    clip_voice = clip_voice.with_start(0)
    clips_audio = [clip_voice]
    
    music_dir = "music"
    if os.path.exists(music_dir) and os.path.isdir(music_dir):
        tracks = sorted([f for f in os.listdir(music_dir) if f.endswith(".mp3")])
        
        if tracks:
            idx = 0
            if os.path.exists(MUSIC_IDX_FILE):
                try:
                    with open(MUSIC_IDX_FILE, "r") as f:
                        idx = int(f.read().strip())
                except:
                    idx = 0
            
            track_name = tracks[idx % len(tracks)]
            chosen_track = os.path.join(music_dir, track_name)
            print(f"Выбрана музыка: {chosen_track}")
            
            with open(MUSIC_IDX_FILE, "w") as f:
                f.write(str((idx + 1) % len(tracks)))
            
            clip_bgm = AudioFileClip(chosen_track).with_volume_scaled(0.1)
            if clip_bgm.duration < duration:
                clip_bgm = clip_bgm.looped(duration=duration)
            else:
                clip_bgm = clip_bgm.subclipped(0, duration)
            clips_audio.append(clip_bgm)
        else:
            print("Музыка не найдена в папке music/")

    final = CompositeVideoClip(clips, size=(W, H))
    final = final.with_audio(CompositeAudioClip(clips_audio))
    
    final.write_videofile(out_path, fps=24, codec="libx264", audio_codec="aac", threads=4, logger=None)
    print("Видео готово:", out_path)
    return out_path

if __name__ == "__main__":
    test_text = "Привет, это тест нейросетевого диктора Silero. Никаких блокировок, только чистый звук. Смотрите, как я пульсирую."
    create_video(test_text, "ТЕСТ ГОЛОСА", "meta_temp.png", "brawler_temp.png")
