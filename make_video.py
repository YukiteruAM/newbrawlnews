import os, sys, asyncio, edge_tts, io, re
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import numpy as np
from moviepy.editor import *

# Настройки видео
W, H = 1080, 1920
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

async def generate_voice(text, out_file="voice.mp3"):
    # Живой русский мужской голос от Microsoft
    voice = "ru-RU-DmitryNeural" 
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(out_file)

def get_base_bg():
    # Рисуем темный неоновый фон 9:16
    bg = Image.new('RGBA', (W, H), (10, 5, 20, 255))
    draw = ImageDraw.Draw(bg)
    
    # Неоновые пятна
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
    print("1. Генерирую голос...")
    asyncio.run(generate_voice(text, "voice.mp3"))
    
    print("2. Готовлю картинки...")
    # Фон
    bg_img = get_base_bg()
    
    # Заголовок
    draw = ImageDraw.Draw(bg_img)
    try:
        font_title = ImageFont.truetype(FONT_PATH, 110)
        font_sub = ImageFont.truetype(FONT_PATH, 70)
    except:
        font_title = font_sub = ImageFont.load_default()
        
    draw.text((W/2, 200), title, font=font_title, fill="#ffcc00", anchor="mm", stroke_width=6, stroke_fill="black")
    
    # Сохраняем фон во временный файл
    bg_img.convert("RGB").save("bg_temp.png")
    
    # Загружаем карточку меты (если есть)
    meta_img = None
    if os.path.exists(img_path):
        meta_img = Image.open(img_path).convert("RGBA")
        # Ресайзим карточку, чтобы влезла
        meta_img.thumbnail((900, 900), Image.Resampling.LANCZOS)
        meta_img.save("meta_temp.png")

    # Загружаем бойца-диктора (например, Эдгара)
    brawler_img = None
    if os.path.exists(brawler_path):
        brawler_img = Image.open(brawler_path).convert("RGBA")
        brawler_img.thumbnail((450, 450), Image.Resampling.LANCZOS)
        brawler_img.save("brawler_temp.png")

    print("3. Собираю видео (это займет пару минут)...")
    # Основной фон
    clip_bg = ImageClip("bg_temp.png").set_duration(15)
    
    clips = [clip_bg]
    
    # Добавляем карточку меты с анимацией "выплывания" снизу
    if os.path.exists("meta_temp.png"):
        clip_meta = (ImageClip("meta_temp.png")
                     .set_duration(15)
                     .set_position(("center", "center"))
                     .crossfadein(1)
                     .resize(lambda t: 1 + 0.05*np.sin(t*2))) # Легкая пульсация
        clips.append(clip_meta)
    
    # Добавляем бойца в правый нижний угол с пульсацией (как будто говорит)
    if os.path.exists("brawler_temp.png"):
        clip_brawler = (ImageClip("brawler_temp.png")
                        .set_duration(15)
                        .set_position((W - 400, H - 450))
                        .resize(lambda t: 1 + 0.03*np.sin(t*10))) # Быстрая пульсация
        clips.append(clip_brawler)
    
    # Аудио: голос + музыка
    clip_voice = AudioFileClip("voice.mp3")
    duration = clip_voice.duration + 0.5 # Добавляем полсекунды в конце
    
    # Обрезаем фоновые клипы под длину голоса
    for c in clips:
        c.duration = duration
        
    clip_voice = clip_voice.set_start(0)
    clips_audio = [clip_voice]
    
    if os.path.exists("bgm.mp3"):
        clip_bgm = AudioFileClip("bgm.mp3").volumex(0.1) # Музыка тише
        # Зацикливаем музыку, если она короче
        if clip_bgm.duration < duration:
            clip_bgm = clip_bgm.loop(duration=duration)
        else:
            clip_bgm = clip_bgm.subclip(0, duration)
        clips_audio.append(clip_bgm)

    # Финальная сборка
    final = CompositeVideoClip(clips, size=(W, H))
    final = final.set_audio(CompositeAudioClip(clips_audio))
    
    final.write_videofile(out_path, fps=24, codec="libx264", audio_codec="aac", threads=4)
    print("4. Видео готово:", out_path)
    return out_path

if __name__ == "__main__":
    # ТЕСТОВЫЙ ЗАПУСК
    # Сюда мы будем передавать текст и пути к картинкам из bot.py
    test_text = "Привет, это тест нейросетевого диктора. Смотрите, как я двигаюсь, пока говорю."
    create_video(test_text, "ТЕСТ ВИДЕО", "meta_temp.png", "brawler_temp.png")
