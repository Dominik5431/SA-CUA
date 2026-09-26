import base64
import math
from io import BytesIO
from typing import Tuple

from PIL import Image

def round_by_factor(number: int, factor: int) -> int:
    """返回最接近 number 的且能被 factor 整除的整数"""
    return round(number / factor) * factor


def ceil_by_factor(number: int, factor: int) -> int:
    """返回大于等于 number 的且能被 factor 整除的整数"""
    return math.ceil(number / factor) * factor


def floor_by_factor(number: int, factor: int) -> int:
    """返回小于等于 number 的且能被 factor 整除的整数"""
    return math.floor(number / factor) * factor


def smart_resize(height, width, factor=28, min_pixels=56 * 56, max_pixels=14 * 14 * 4 * 1280, max_long_side=8192):
    """缩放后图片满足以下条件:
    1. 长宽能被 factor 整除
    2. pixels 总数被限制在 [min_pixels, max_pixels] 内
    3. 最长边限制在 max_long_side 内
    4. 保证其长宽比基本不变
    """
    if height < 2 or width < 2:
        raise ValueError(f"height:{height} or width:{width} must be larger than factor:{factor}")
    elif max(height, width) / min(height, width) > 200:
        raise ValueError(f"absolute aspect ratio must be smaller than 100, got {height} / {width}")

    if max(height, width) > max_long_side:
        beta = max(height, width) / max_long_side
        height, width = int(height / beta), int(width / beta)

    h_bar = round_by_factor(height, factor)
    w_bar = round_by_factor(width, factor)
    if h_bar * w_bar > max_pixels:
        beta = math.sqrt((height * width) / max_pixels)
        h_bar = floor_by_factor(height / beta, factor)
        w_bar = floor_by_factor(width / beta, factor)
    elif h_bar * w_bar < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h_bar = ceil_by_factor(height * beta, factor)
        w_bar = ceil_by_factor(width * beta, factor)
    return h_bar, w_bar

def process_image(image_bytes: bytes) -> str:
    """Resize + re-encode screenshot and return base64 PNG."""
    image = Image.open(BytesIO(image_bytes))
    width, height = image.size

    resized_height, resized_width = smart_resize(
        height=height,
        width=width,
        factor=32,
        max_pixels=16 * 16 * 4 * 12800,
    )

    image = image.resize((resized_width, resized_height))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def image_size_from_bytes(image_bytes: bytes) -> Tuple[int, int]:
    image = Image.open(BytesIO(image_bytes))
    return image.size


def image_size_from_base64(image_b64: str) -> Tuple[int, int]:
    image = Image.open(BytesIO(base64.b64decode(image_b64)))
    return image.size


def adjust_coordinates(
    x: float,
    y: float,
    *,
    coordinate_type: str,
    original_width: int = None,
    original_height: int = None,
    processed_width: int = None,
    processed_height: int = None,
) -> Tuple[int, int]:
    if not (original_width and original_height):
        return int(x), int(y)
    if coordinate_type == "absolute":
        if processed_width and processed_height:
            x_scale = original_width / processed_width
            y_scale = original_height / processed_height
            return int(x * x_scale), int(y * y_scale)
        return int(x), int(y)

    x_scale = original_width / 999
    y_scale = original_height / 999
    return int(x * x_scale), int(y * y_scale)
