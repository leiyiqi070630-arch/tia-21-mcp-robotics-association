"""生成桌面端图标：侧栏透明 LOGO、圆角白底应用图标（PNG + 多尺寸 ICO）。

源图：build/assets/tia-logo-source.png（黑字、透明背景）。
输出：
  desktop/src/renderer/assets/logo-tia.png  侧栏透明 LOGO（暗色主题由 CSS 反转为白色）
  desktop/src/renderer/assets/icon.png      256px 圆角白底应用图标（窗口/任务栏）
  desktop/build/icon.ico                    electron-builder 用的多尺寸 ico
  build/assets/app-icon.ico                 启动器与 MCP 服务 exe 用的 ico

只依赖 Pillow（构建期依赖，已列入 requirements-dev.txt）。
用法：.venv\\Scripts\\python.exe build\\make_icons.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT_ROOT / "build" / "assets" / "tia-logo-source.png"
ICON_DIR = PROJECT_ROOT / "desktop" / "build"
ASSET_DIR = PROJECT_ROOT / "desktop" / "src" / "renderer" / "assets"
PUBLIC_ICON = PROJECT_ROOT / "build" / "assets" / "app-icon.ico"
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
APP_ICON_SIZE = 256
SUPERSAMPLE = 4
# 圆角白底：半径约为边长的 22%，接近 Windows 11 / DSH 桌面图标的观感。
CORNER_RATIO = 0.225
# 图标中 LOGO 占画布宽度的比例（icon 文件里的字标比侧栏更大，避免 exe 图标看起来留白过多）。
LOGO_WIDTH_RATIO = 0.80
# 横向轻微压缩：宽度按比例取，高度按 1/SQUEEZE 拉高，字标更紧凑也更醒目。
LOGO_SQUEEZE = 0.93
# 缩小后做一点反锐化，抵消 LANCZOS 带来的边缘变软。
SHARPEN_PERCENT = {16: 0, 24: 25, 32: 35, 48: 40, 64: 45, 128: 45, 256: 45}


def _load_logo() -> Image.Image:
    if not SOURCE.is_file():
        raise SystemExit(f"缺少源图标：{SOURCE}")
    image = Image.open(SOURCE).convert("RGBA")
    # 源图四角有 alpha=1 的极淡噪点，先按阈值清掉再裁边，否则裁出来几乎是整张画布。
    alpha = image.getchannel("A").point(lambda value: value if value >= 24 else 0)
    image.putalpha(alpha)
    alpha_box = alpha.getbbox()
    if not alpha_box:
        raise SystemExit(f"源图标没有可见像素：{SOURCE}")
    return image.crop(alpha_box)


def _icon(size: int, logo: Image.Image) -> Image.Image:
    """生成指定尺寸的圆角白底图标：4 倍超采样排版后一次性缩小，再轻微反锐化。"""
    big = size * SUPERSAMPLE
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    radius = int(big * CORNER_RATIO)
    ImageDraw.Draw(canvas).rounded_rectangle((0, 0, big - 1, big - 1), radius=radius, fill=(255, 255, 255, 255))

    logo_width = int(big * LOGO_WIDTH_RATIO)
    logo_height = max(1, round(logo.height * logo_width / logo.width / LOGO_SQUEEZE))
    resized = logo.resize((logo_width, logo_height), Image.LANCZOS)
    canvas.alpha_composite(resized, ((big - logo_width) // 2, (big - logo_height) // 2))
    icon = canvas.resize((size, size), Image.LANCZOS)

    percent = SHARPEN_PERCENT.get(size, 40)
    if percent:
        alpha = icon.getchannel("A")
        rgb = icon.convert("RGB").filter(ImageFilter.UnsharpMask(radius=1.0, percent=percent, threshold=2))
        icon = Image.merge("RGBA", (*rgb.split(), alpha))
    return icon


def main() -> int:
    logo = _load_logo()
    print(f"源 LOGO：{logo.width}x{logo.height}")

    ICON_DIR.mkdir(parents=True, exist_ok=True)
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    PUBLIC_ICON.parent.mkdir(parents=True, exist_ok=True)

    sidebar = logo.resize((512, max(1, round(logo.height * 512 / logo.width))), Image.LANCZOS)
    sidebar_path = ASSET_DIR / "logo-tia.png"
    sidebar.save(sidebar_path, format="PNG")
    print(f"已生成侧栏 LOGO：{sidebar_path}（{sidebar.width}x{sidebar.height}）")

    window_icon = ASSET_DIR / "icon.png"
    _icon(APP_ICON_SIZE, logo).save(window_icon, format="PNG")
    print(f"已生成窗口图标：{window_icon}")

    frames = {size: _icon(size, logo) for size in ICO_SIZES}
    ico_path = ICON_DIR / "icon.ico"
    frames[256].save(
        ico_path,
        format="ICO",
        sizes=[(size, size) for size in ICO_SIZES],
        append_images=[frames[size] for size in ICO_SIZES if size != 256],
    )
    print(f"已生成：{ico_path}（{', '.join(str(size) for size in ICO_SIZES)}）")

    shutil.copyfile(ico_path, PUBLIC_ICON)
    print(f"已生成：{PUBLIC_ICON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
