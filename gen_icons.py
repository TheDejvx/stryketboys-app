from PIL import Image, ImageDraw, ImageFont
import os

NAVY = (10, 37, 64, 255)
NAVY_DARK = (8, 28, 48, 255)
WHITE = (255, 255, 255, 255)

def find_font(size):
    candidates = [
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ]
    for c in candidates:
        if os.path.exists(c):
            return ImageFont.truetype(c, size)
    return ImageFont.load_default()

def make_icon(size, path, radius_ratio=0.22):
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    radius = int(size * radius_ratio)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=NAVY)

    # subtle bottom-shadow band for depth
    draw.rounded_rectangle([0, int(size * 0.72), size - 1, size - 1], radius=radius, fill=NAVY_DARK)
    draw.rectangle([0, int(size * 0.72), size - 1, int(size * 0.82)], fill=NAVY_DARK)

    text = "SB"
    font = find_font(int(size * 0.42))
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1] - size * 0.03), text, font=font, fill=WHITE)

    img.save(path)
    print(f"wrote {path}")

base = os.path.dirname(__file__)
make_icon(192, os.path.join(base, 'icon-192.png'))
make_icon(512, os.path.join(base, 'icon-512.png'))
make_icon(180, os.path.join(base, 'icon-180.png'), radius_ratio=0.0)  # apple-touch-icon: square, iOS applies its own mask
