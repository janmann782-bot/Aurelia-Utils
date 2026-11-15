"""Deterministic geography. No AI, network access or full-size RGB arrays.

Seed geography is evaluated on a 2048-wide field then sampled in row strips.
Sketch strict mode bypasses this field and preserves every classified source pixel.
"""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import math
import os
import struct
import zlib
import warnings

import numpy as np
from PIL import Image, UnidentifiedImageError
from scipy.ndimage import map_coordinates, distance_transform_edt

SEA = (36, 112, 202)
LAND = (255, 255, 255)
ENGINE_VERSION = "1"
MAX_PIXELS = 16384 * 8192
MAX_INPUT_PIXELS = 8192 * 4096
Image.MAX_IMAGE_PIXELS = MAX_INPUT_PIXELS


@dataclass(frozen=True)
class Params:
    seed: str = "Aurelia"
    width: int = 2048
    height: int = 1024
    land: int = 35
    continents: int = 5
    roughness: int = 55

    def validate(self):
        if not 64 <= self.width <= 16384 or not 32 <= self.height <= 16384:
            raise ValueError("Сторона карты вне диапазона 64-16 384 px")
        if self.width * self.height > MAX_PIXELS:
            raise ValueError("Карта больше 134 217 728 px")
        if not 0.25 <= self.width / self.height <= 4:
            raise ValueError("Соотношение сторон должно быть от 1:4 до 4:1")
        if not 5 <= self.land <= 90 or not 1 <= self.continents <= 12:
            raise ValueError("Суша: 5-90 %, материковые ядра: 1-12")
        if not 0 <= self.roughness <= 100 or not 1 <= len(self.seed) <= 128:
            raise ValueError("Берег: 0-100, seed: 1-128 символов")


def seed_number(seed: str) -> int:
    return int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8], "big")


def noise(shape, rng, roughness=55):
    """Cubic multiscale noise with periodic longitude and mirrored latitude."""
    h, w = shape
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    field = np.zeros(shape, np.float32)
    for octave, columns in enumerate((4, 8, 16, 32, 64, 128, 256)):
        rows = max(2, round(columns * h / w))
        grid = rng.standard_normal((rows + 1, columns + 1)).astype(np.float32)
        grid[:, -1] = grid[:, 0]
        sample = map_coordinates(grid, [yy / max(1, h-1) * rows, xx / w * columns],
                                 order=3, mode="nearest", prefilter=True)
        persistence = 0.39 + roughness / 100 * 0.23
        field += sample * persistence**octave
    field -= field.mean()
    field /= max(float(field.std()), 1e-6)
    return field


def geography(p: Params, progress=lambda *_: None):
    p.validate()
    scale = min(1., 2048 / max(p.width,p.height))
    w = max(32,round(p.width*scale))
    h = max(32,round(p.height*scale))
    rng = np.random.default_rng(seed_number(p.seed))
    progress("Собираю основу материков", 0, 1)
    y, x = np.mgrid[:h, :w].astype(np.float32)
    x /= w
    y /= h
    # Domain warping breaks the Gaussian outlines before coast detail is added.
    warp_x = noise((h, w), rng, p.roughness) * .035
    warp_y = noise((h, w), rng, p.roughness) * .045
    x += warp_x
    y += warp_y
    core = np.full((h, w), -10, np.float32)
    for i in range(p.continents):
        cx = ((i + .5) / p.continents + rng.uniform(-.1, .1)) % 1
        cy = rng.uniform(.21, .79)
        rx = rng.uniform(.11, .20) * (5 / p.continents)**.25
        ry = rng.uniform(.14, .30)
        dx = (x-cx+.5) % 1-.5
        dy = y-cy
        angle = rng.uniform(-.7, .7)
        u = (dx * math.cos(angle) + dy * math.sin(angle)) / rx
        v = (-dx * math.sin(angle) + dy * math.cos(angle)) / ry
        blob = 1 - (u*u + v*v)
        np.maximum(core, blob, out=core)
    detail = noise((h, w), rng, p.roughness)
    core += detail * (.22 + p.roughness / 100 * .40)
    # Avoid a hard rectangular land cap on the map edges.
    core -= (np.abs(y-.5) * 2)**8 * .65
    threshold = float(np.quantile(core, 1-p.land/100))
    core -= threshold
    progress("Основа материков готова", 1, 1)
    return core


class PNGWriter:
    """Atomic streaming RGB PNG, O(width * strip_height) output memory."""
    def __init__(self, path: Path, width: int, height: int):
        self.path = Path(path)
        self.temp = self.path.with_suffix(self.path.suffix + ".part")
        self.width, self.height, self.rows = width, height, 0
        self.file = self.temp.open("wb")
        self.file.write(b"\x89PNG\r\n\x1a\n")
        self.chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        self.zip = zlib.compressobj(6)

    def chunk(self, kind, data):
        self.file.write(struct.pack(">I", len(data)) + kind + data)
        self.file.write(struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))

    def write(self, rgb):
        if rgb.ndim != 3 or rgb.shape[1:] != (self.width, 3):
            raise ValueError("Invalid PNG strip")
        for row in rgb:
            block = self.zip.compress(b"\0" + row.astype(np.uint8, copy=False).tobytes())
            if block:
                self.chunk(b"IDAT", block)
        self.rows += len(rgb)

    def close(self):
        try:
            if self.rows != self.height:
                raise ValueError("Incomplete PNG")
            self.chunk(b"IDAT", self.zip.flush())
            self.chunk(b"IEND", b"")
            self.file.flush()
            os.fsync(self.file.fileno())
            self.file.close()
            os.replace(self.temp, self.path)
        except BaseException:
            self.abort()
            raise

    def abort(self):
        self.file.close()
        self.temp.unlink(missing_ok=True)


def rgb_mask(mask):
    rgb = np.empty((*mask.shape, 3), np.uint8)
    rgb[:] = SEA
    rgb[mask] = LAND
    return rgb


def render_field(field, width, height, path, progress=lambda *_: None):
    """Use periodic x sampling; the first/last columns are neighboring longitudes."""
    writer = PNGWriter(path, width, height)
    # One wrapped padding column makes interpolation across the longitude seam explicit.
    padded = np.concatenate([field, field[:, :1]], axis=1)
    count = 0
    try:
        xs = np.arange(width, dtype=np.float32) * field.shape[1] / width
        for start in range(0, height, 64):
            end = min(start+64, height)
            ys = np.arange(start, end, dtype=np.float32) * (field.shape[0]-1) / max(1, height-1)
            yy, xx = np.meshgrid(ys, xs, indexing="ij")
            mask = map_coordinates(padded, [yy, xx], order=1, prefilter=False, mode="nearest") >= 0
            writer.write(rgb_mask(mask))
            count += int(mask.sum())
            progress("Собираю PNG", end, height)
        writer.close()
    except BaseException:
        writer.abort()
        raise
    return count / (width*height)


def inspect_image(path: Path):
    """Check content, not extension. Never execute user-provided files."""
    if path.stat().st_size > 20_000_000:
        raise ValueError("Файл больше 20 МБ")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as im:
                if im.format not in {"PNG", "JPEG", "WEBP", "BMP"}:
                    raise ValueError("Пришли PNG, JPEG, WEBP или BMP")
                if getattr(im, "n_frames", 1) != 1:
                    raise ValueError("Нужна обычная картинка без анимации")
                if im.width*im.height > MAX_INPUT_PIXELS:
                    raise ValueError("Для пятна максимум 33 554 432 px, например 8 192 x 4 096")
                if max(im.size) > 16384 or min(im.size) < 32:
                    raise ValueError("Стороны исходника: от 32 до 16 384 px")
                if not .25 <= im.width/im.height <= 4:
                    raise ValueError("Соотношение сторон исходника: от 1:4 до 4:1")
                size = im.size
                im.verify()
        return size
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as e:
        raise ValueError("Не могу прочитать картинку. Попробуй отправить PNG документом") from e


def sketch_mask(path: Path, polarity="dark", threshold=128):
    inspect_image(path)
    if polarity not in {"dark", "light"} or not 1 <= threshold <= 254:
        raise ValueError("Некорректный выбор суши")
    with Image.open(path) as source:
        # EXIF orientation is deliberately not applied: keep the encoded pixel grid.
        gray = source.convert("L")
        mask = np.asarray(gray) < threshold if polarity == "dark" else np.asarray(gray) >= threshold
        if "A" in source.getbands() or "transparency" in source.info:
            alpha = np.asarray(source.convert("RGBA").getchannel("A"))
            mask &= alpha >= 128
    if not mask.any() or mask.all():
        raise ValueError("Получилась только вода или только суша. Поменяй порог или выбор цвета")
    return mask


def render_strict(mask, width, height, path, progress=lambda *_: None):
    """Nearest-neighbor pixel mapping. No morphology, denoise or lost source pixels at 1:1."""
    writer = PNGWriter(path, width, height)
    count = 0
    xs = np.minimum(np.arange(width, dtype=np.int64)*mask.shape[1]//width, mask.shape[1]-1)
    try:
        for start in range(0, height, 64):
            ys = np.minimum(np.arange(start, min(start+64, height), dtype=np.int64)*mask.shape[0]//height,
                            mask.shape[0]-1)
            strip = mask[np.ix_(ys, xs)]
            count += int(strip.sum())
            writer.write(rgb_mask(strip))
            progress("Сохраняю пиксели пятна", min(start+64, height), height)
        writer.close()
    except BaseException:
        writer.abort()
        raise
    return count / (width*height)


def sketch_field(mask, params):
    """Explicit coast variation, bounded to 2.4% of working image width.

    Interiors remain in place beyond that distance. Small islands within this
    band may change, as explained in the Telegram confirmation panel.
    """
    scale = min(1., 2048/max(mask.shape))
    w = max(32,round(mask.shape[1]*scale))
    h = max(32,round(mask.shape[0]*scale))
    small = np.asarray(Image.fromarray(mask).resize((w,h),Image.Resampling.NEAREST))
    signed = (distance_transform_edt(small)-distance_transform_edt(~small)).astype(np.float32)
    rng = np.random.default_rng(seed_number(params.seed))
    coast = noise((h,w),rng,max(75,params.roughness))
    signed += np.clip(coast,-2.,2.) * w * .012
    return signed


def render_sketch(path, params, output, polarity="dark", threshold=128,
                  mode="strict", progress=lambda *_: None):
    params.validate()
    mask = sketch_mask(Path(path), polarity, threshold)
    source_size = (mask.shape[1], mask.shape[0])
    if abs(params.width/params.height - source_size[0]/source_size[1]) > .002:
        raise ValueError("Пропорции пятна менять нельзя")
    if params.width < source_size[0] or params.height < source_size[1]:
        raise ValueError("Уменьшение может уничтожить острова. Выбери исходный размер или больше")
    if mode == "strict":
        share = render_strict(mask, params.width, params.height, output, progress)
    elif mode == "coast":
        field = sketch_field(mask, params)
        share = render_field(field, params.width, params.height, output, progress)
    else:
        raise ValueError("Неизвестный режим пятна")
    return share


def preview_mask(source, output, polarity, threshold=128):
    mask = sketch_mask(Path(source), polarity, threshold)
    im = Image.fromarray(rgb_mask(mask)) if mask.size <= 2_000_000 else None
    if im is None:
        small = Image.fromarray(mask)
        small.thumbnail((1280, 1280), Image.Resampling.NEAREST)
        im = Image.fromarray(rgb_mask(np.asarray(small)))
    im.thumbnail((1280, 1280), Image.Resampling.NEAREST)
    im.save(output)

