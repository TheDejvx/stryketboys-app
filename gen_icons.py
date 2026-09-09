from PIL import Image
import os

base = os.path.dirname(__file__)
SOURCE = os.path.join(base, 'static', 'logo-source.jpg')


def make_icon(square, size, path):
    resized = square.resize((size, size), Image.LANCZOS)
    resized.save(path)
    print(f"wrote {path}")


src = Image.open(SOURCE).convert('RGB')
w, h = src.size
side = min(w, h)
left = (w - side) // 2
top = (h - side) // 2
square = src.crop((left, top, left + side, top + side))

make_icon(square, 192, os.path.join(base, 'static', 'icon-192.png'))
make_icon(square, 512, os.path.join(base, 'static', 'icon-512.png'))
make_icon(square, 180, os.path.join(base, 'static', 'icon-180.png'))
