#!/usr/bin/env python3
"""图形验证码（阶段 5 工作包 E，登录验证码，可选开启）。

  - 开关：web/config.py 的 CAPTCHA_ENABLED（环境变量
    RBOT_CAPTCHA_ENABLED=1 才开启），默认关闭，登录流程不变。
  - 生成：5 位数字/字母（去掉易混淆的 0/O/1/I/l），Pillow 画 PNG；
    Pillow 未安装时用 SVG 字符画兜底（纯标准库）。
  - 存储：内存字典 captcha_id -> {answer, expires}，5 分钟过期，
    校验成功后立即删除（一次性），校验失败同样删除（防暴力猜测）。
  - 安全：验证码答案绝不写日志；比较用 hmac.compare_digest。

接口：
  - generate_captcha() -> (captcha_id, answer, image_bytes, mime)
    image_bytes 是 PNG 或 SVG 字节，mime 是对应类型。
  - verify_captcha(captcha_id, answer) -> bool（一次性，过期返回 False）。
"""

import base64
import hmac
import io
import random
import secrets
import string
import time

# ================= 配置区（改这里） =================
# 验证码字符集：去掉易混淆的 0/O/1/I/l。
CAPTCHA_CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz"
# 验证码长度（字符数）。
CAPTCHA_LENGTH = 5
# 验证码有效期（秒）：5 分钟。
CAPTCHA_EXPIRE_SECONDS = 5 * 60
# 图片尺寸（像素）。
CAPTCHA_WIDTH = 160
CAPTCHA_HEIGHT = 60
# =====================================================

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - Pillow 未安装时的降级分支
    _PIL_AVAILABLE = False


def _random_answer(length=CAPTCHA_LENGTH):
    """生成随机验证码答案（secrets 随机，不用 random）。"""
    return "".join(secrets.choice(CAPTCHA_CHARS) for _ in range(length))


def _render_png(answer):
    """用 Pillow 画 PNG 验证码：白底 + 随机颜色字符 + 干扰线/噪点。"""
    image = Image.new("RGB", (CAPTCHA_WIDTH, CAPTCHA_HEIGHT), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    rng = random.Random()
    # 干扰线。
    for _ in range(4):
        draw.line(
            [(rng.randint(0, CAPTCHA_WIDTH), rng.randint(0, CAPTCHA_HEIGHT)),
             (rng.randint(0, CAPTCHA_WIDTH), rng.randint(0, CAPTCHA_HEIGHT))],
            fill=(rng.randint(150, 220), rng.randint(150, 220),
                  rng.randint(150, 220)),
            width=1)
    # 字符：每个字符随机颜色与纵向偏移。
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 34)
    except (OSError, IOError):
        font = ImageFont.load_default()
    x = 12
    for ch in answer:
        draw.text((x, rng.randint(6, 16)), ch,
                  fill=(rng.randint(0, 120), rng.randint(0, 120),
                        rng.randint(0, 120)),
                  font=font)
        x += (CAPTCHA_WIDTH - 24) // max(1, len(answer))
    # 噪点。
    for _ in range(60):
        draw.point((rng.randint(0, CAPTCHA_WIDTH - 1),
                    rng.randint(0, CAPTCHA_HEIGHT - 1)),
                   fill=(rng.randint(180, 230), rng.randint(180, 230),
                         rng.randint(180, 230)))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue(), "image/png"


def _render_svg(answer):
    """Pillow 未安装时的兜底：SVG 字符画（纯标准库生成）。"""
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{CAPTCHA_WIDTH}" height="{CAPTCHA_HEIGHT}" '
        f'viewBox="0 0 {CAPTCHA_WIDTH} {CAPTCHA_HEIGHT}">',
        f'<rect width="{CAPTCHA_WIDTH}" height="{CAPTCHA_HEIGHT}" '
        f'fill="#ffffff"/>',
    ]
    rng = random.Random()
    for _ in range(3):
        parts.append(
            f'<line x1="{rng.randint(0, CAPTCHA_WIDTH)}" '
            f'y1="{rng.randint(0, CAPTCHA_HEIGHT)}" '
            f'x2="{rng.randint(0, CAPTCHA_WIDTH)}" '
            f'y2="{rng.randint(0, CAPTCHA_HEIGHT)}" '
            f'stroke="#cccccc" stroke-width="1"/>')
    x = 14
    for ch in answer:
        color = "#%02x%02x%02x" % (rng.randint(0, 120), rng.randint(0, 120),
                                   rng.randint(0, 120))
        parts.append(
            f'<text x="{x}" y="{rng.randint(32, 48)}" '
            f'font-family="monospace" font-size="32" font-weight="bold" '
            f'fill="{color}">{ch}</text>')
        x += (CAPTCHA_WIDTH - 28) // max(1, len(answer))
    parts.append("</svg>")
    return "".join(parts).encode("utf-8"), "image/svg+xml"


def render_captcha_image(answer):
    """渲染验证码图片，返回 (图片字节, MIME 类型)。

    Pillow 可用时画 PNG，否则用 SVG 字符画兜底。
    """
    if _PIL_AVAILABLE:
        return _render_png(answer)
    return _render_svg(answer)


class CaptchaStore:
    """验证码内存存储：captcha_id -> {answer, expires}。

    5 分钟过期；verify() 无论成功失败都删除该条（一次性，防重放与
    暴力猜测）。过期条目在 verify/get 时顺带清理。
    """

    def __init__(self, expire_seconds=CAPTCHA_EXPIRE_SECONDS):
        self.expire_seconds = expire_seconds
        self._codes = {}

    def create(self, answer=None):
        """生成一条验证码，返回 (captcha_id, answer)。

        answer 不传时随机生成；传了则用指定答案（测试用）。
        """
        captcha_id = secrets.token_urlsafe(16)
        answer = answer if answer is not None else _random_answer()
        self._codes[captcha_id] = {
            "answer": answer,
            "expires": time.time() + self.expire_seconds,
        }
        return captcha_id, answer

    def verify(self, captcha_id, answer):
        """校验验证码（一次性）：成功返回 True，否则 False。

        captcha_id 不存在、已过期、答案不符都返回 False；
        校验后无论结果都删除该条。答案比较大小写不敏感，
        用 hmac.compare_digest 做恒定时间比较。
        答案绝不写日志。
        """
        if not captcha_id or not answer:
            return False
        record = self._codes.pop(captcha_id, None)
        if record is None:
            return False
        if record["expires"] <= time.time():
            return False
        expected = record["answer"]
        if not isinstance(expected, str):
            return False
        return hmac.compare_digest(expected.lower(), str(answer).lower())

    def reset(self):
        """清空全部验证码（测试用）。"""
        self._codes.clear()


# 全局单例（单进程内共享）。
CAPTCHAS = CaptchaStore()


def generate_captcha():
    """生成验证码，返回 (captcha_id, data_url)。

    data_url 是 "data:<mime>;base64,..." 形式，前端直接当 <img> src 用。
    答案存在 CAPTCHAS 里（5 分钟过期，一次性），不返回给调用方之外的
    任何地方（本函数只返回 id 与图片，答案不出内存字典）。
    """
    captcha_id, answer = CAPTCHAS.create()
    image_bytes, mime = render_captcha_image(answer)
    data_url = ("data:" + mime + ";base64,"
                + base64.b64encode(image_bytes).decode("ascii"))
    return captcha_id, data_url
