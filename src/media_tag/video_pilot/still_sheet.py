"""Pack verified independent stills into one labelled JPEG for a vision request."""

from hashlib import sha256
from io import BytesIO
import json
import math

from PIL import Image, ImageDraw, ImageFont


def make_still_sheet(folder, stills, edge):
    if edge not in (320, 640) or not 1 <= len(stills) <= 8:
        raise ValueError('invalid still sheet size or frame count')
    tiles = []
    for entry, raw in stills:
        with Image.open(BytesIO(raw)) as source:
            source.thumbnail((edge, edge), Image.Resampling.LANCZOS)
            tiles.append((entry, source.convert('RGB')))

    font_size = 16 if edge == 320 else 22
    label_height = font_size + 8
    width = max(120, max(tile.width for _, tile in tiles))
    height = max(tile.height for _, tile in tiles) + label_height
    columns = min(len(tiles), max(1, round(math.sqrt(len(tiles) * height / width))))
    rows = math.ceil(len(tiles) / columns)
    sheet = Image.new('RGB', (columns * width, rows * height))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=font_size)
    cells = []
    for index, (entry, tile) in enumerate(tiles):
        row, column = divmod(index, columns)
        x, y = column * width, row * height
        sheet.paste(tile, (x + (width - tile.width) // 2, y + (height - label_height - tile.height) // 2))
        draw.text((x + 3, y + height - label_height), f"{entry['id']}   {entry['start']:.2f} s",
                  fill='white', font=font)
        cells.append(dict(id=entry['id'], seconds=entry['start'], source_sha256=entry['sha256'],
                          row=row, column=column))

    encoded = BytesIO()
    sheet.save(encoded, format='JPEG', quality=87)
    raw = encoded.getvalue()
    (folder / 'still-sheet.jpg').write_bytes(raw)
    record = dict(path='still-sheet.jpg', sha256=sha256(raw).hexdigest(), bytes=len(raw),
                  tile_edge=edge, width=sheet.width, height=sheet.height, columns=columns, cells=cells)
    (folder / 'still-sheet.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    return raw
