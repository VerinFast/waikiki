from PIL import Image, ImageDraw, ImageFont
FONT = "/System/Library/Fonts/Apple Color Emoji.ttc"

def emoji_img(ch, px):
    # Apple Color Emoji renders at fixed strikes; 160 is a supported size.
    f = ImageFont.truetype(FONT, 160)
    im = Image.new("RGBA", (176, 176), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.text((88, 88), ch, font=f, anchor="mm", embedded_color=True)
    return im.resize((px, px), Image.LANCZOS)

SZ = 1024
canvas = Image.new("RGBA", (SZ, SZ), (0, 0, 0, 0))
floppy = emoji_img("\U0001F4BE", int(SZ * 0.86))          # 💾 background
canvas.alpha_composite(floppy, ((SZ - floppy.width) // 2, (SZ - floppy.height) // 2))
flower = emoji_img("\U0001F33A", int(SZ * 0.42))           # 🌺 centered on the label
canvas.alpha_composite(flower, ((SZ - flower.width) // 2, int(SZ * 0.40)))
canvas.save("assets/icon-1024.png")

# Build .iconset with the sizes macOS wants.
import os, subprocess
os.makedirs("assets/Waikiki.iconset", exist_ok=True)
for size in (16, 32, 128, 256, 512):
    for scale, suffix in ((1, ""), (2, "@2x")):
        px = size * scale
        canvas.resize((px, px), Image.LANCZOS).save(
            f"assets/Waikiki.iconset/icon_{size}x{size}{suffix}.png")
subprocess.run(["iconutil", "-c", "icns", "assets/Waikiki.iconset",
                "-o", "assets/Waikiki.icns"], check=True)
print("wrote assets/Waikiki.icns")
