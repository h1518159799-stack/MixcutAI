from __future__ import annotations

import re
import unicodedata


LANGUAGE_VOICE_PREFIXES = {
    "zh-CN": ("zh-CN-",),
    "en-US": ("en-US-", "en-GB-", "en-AU-"),
    "es-ES": ("es-ES-", "es-MX-", "es-US-"),
    "vi-VN": ("vi-VN-",),
}

SPANISH_WORDS = {
    "el", "la", "los", "las", "una", "uno", "para", "por", "con", "como",
    "somos", "nuestro", "nuestra", "producto", "productos", "fabrica", "fábrica",
    "calidad", "servicio", "años", "desde", "este", "esta", "que", "del",
}
VIETNAMESE_WORDS = {
    "chúng", "tôi", "của", "và", "là", "cho", "với", "sản", "phẩm", "nhà",
    "máy", "dịch", "vụ", "khách", "hàng", "này", "được", "trong", "một", "có",
}


def contains_cjk(text: str) -> bool:
    return any("\u3400" <= char <= "\u9fff" for char in str(text or ""))


def detect_text_language(text: str) -> str:
    """Detect the supported speech locale without adding a heavy dependency."""
    value = str(text or "").strip()
    if not value:
        return "zh-CN"
    if contains_cjk(value):
        return "zh-CN"

    lowered = value.casefold()
    words = re.findall(r"[^\W\d_]+", lowered, flags=re.UNICODE)
    normalized = unicodedata.normalize("NFD", lowered)
    vietnamese_marks = sum(char in "ăâđêôơư" for char in lowered) + sum(
        unicodedata.name(char, "").startswith(("COMBINING BREVE", "COMBINING HORN"))
        for char in normalized
    )
    if vietnamese_marks or sum(word in VIETNAMESE_WORDS for word in words) >= 2:
        return "vi-VN"
    spanish_marks = sum(char in "ñáéíóúü¿¡" for char in lowered)
    if spanish_marks or sum(word in SPANISH_WORDS for word in words) >= 2:
        return "es-ES"
    return "en-US"


def voice_matches_language(voice_code: str, language: str) -> bool:
    code = str(voice_code or "")
    return any(code.startswith(prefix) for prefix in LANGUAGE_VOICE_PREFIXES.get(language, ()))

