from typing import Dict

import numpy as np
from PIL import Image


def strip_metadata(img: Image.Image) -> Image.Image:
    """
    Remove EXIF and all metadata from the image.
    """
    data = list(img.getdata())
    clean_img = Image.new(img.mode, img.size)
    clean_img.putdata(data)
    return clean_img


def add_imperceptible_noise(img: Image.Image) -> Image.Image:
    """
    Add tiny random noise to each pixel to alter pHash while keeping the image visually identical.
    """
    arr = np.array(img).astype(np.int16)
    noise = np.random.randint(-2, 3, arr.shape, dtype=np.int16)
    arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
    return Image.fromarray(arr)


def micro_crop(img: Image.Image) -> Image.Image:
    """
    Crop 1px from right and bottom edges to change the resolution slightly.
    """
    w, h = img.size
    if w <= 2 or h <= 2:
        return img
    return img.crop((0, 0, w - 1, h - 1))


def anonymize_image(input_path: str, output_path: str) -> str:
    """
    Apply the full anonymization pipeline and save to output_path.
    Returns output_path.
    """
    img = Image.open(input_path).convert("RGB")
    img = strip_metadata(img)
    img = add_imperceptible_noise(img)
    img = micro_crop(img)
    img.save(output_path, format="JPEG", quality=91)
    return output_path

