"""
barcode_utils.py
----------------
أدوات مشتركة لمعالجة قيم الباركود / QR Code قبل ما تتبعت للباقي.

المشكلة:
    بعض الـ QR Codes بتيجي كـ URL كامل، مثلاً:
        https://go.fresh.com.eg/vb5vm?SN=2511TL005663ISI

    واحنا عايزين بس الـ Serial Number: 2511TL005663ISI

الحل:
    دالة normalize_barcode() بتشيل الـ URL وتجيب الـ SN (أو أي parameter تاني)
    وترجع القيمة النظيفة.

    لو المسح مش URL خالص، بترجع القيمة زي ما هي.

استخدام:
    from barcode_utils import normalize_barcode
    clean = normalize_barcode(raw_scan)
"""

from urllib.parse import urlparse, parse_qs


# ─── اسم الـ parameter اللي بيحتوي على الـ Serial Number ─────────────────────
# غيّره لو الشركة التانية بتستخدم اسم مختلف (مثلاً "serial", "id", "code")
_SN_PARAM = "SN"

# ─── لو عايز تضيف شركات تانية بـ parameter مختلف ────────────────────────────
# كل entry: (domain أو جزء من الـ URL, اسم الـ parameter)
_DOMAIN_PARAM_MAP = [
    ("go.fresh.com.eg", "SN"),
    # ("example.com",   "serial"),   # ← مثال لو فيه موردّ تاني
]


def normalize_barcode(raw: str) -> str:
    """
    ينظّف قيمة الباركود / QR Code قبل ما تتبعت للنظام.

    - لو URL:   يستخرج الـ SN (أو أي query param) ويرجعه
    - لو مش URL: يرجع القيمة كما هي (بعد trim)

    مثال:
        normalize_barcode("https://go.fresh.com.eg/vb5vm?SN=2511TL005663ISI")
        → "2511TL005663ISI"

        normalize_barcode("2511TL005663ISI")
        → "2511TL005663ISI"

        normalize_barcode("  ABC123  ")
        → "ABC123"
    """
    raw = raw.strip()
    if not raw:
        return raw

    # هل هو URL؟
    if not raw.lower().startswith(("http://", "https://")):
        return raw  # باركود عادي — نرجعه زي ما هو

    try:
        parsed = urlparse(raw)
        params = parse_qs(parsed.query, keep_blank_values=False)

        if not params:
            # URL بدون query string — نرجع path الأخير
            path_part = parsed.path.rstrip("/").rsplit("/", 1)[-1]
            return path_part if path_part else raw

        # ─── نحاول نعرف الـ parameter الصح بناءً على الـ domain ───
        domain = parsed.netloc.lower()
        for domain_hint, param_name in _DOMAIN_PARAM_MAP:
            if domain_hint in domain and param_name in params:
                value = params[param_name][0].strip()
                if value:
                    return value

        # ─── Fallback: نجرب الـ param الافتراضي SN ───
        if _SN_PARAM in params:
            value = params[_SN_PARAM][0].strip()
            if value:
                return value

        # ─── آخر حاجة: أول param موجود ───
        first_key = next(iter(params))
        return params[first_key][0].strip()

    except Exception:
        # لو فيه أي خطأ في parsing نرجع الـ raw
        return raw


# ─── اختبار سريع ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    tests = [
        # (input,                                                    expected_output)
        ("https://go.fresh.com.eg/vb5vm?SN=2511TL005663ISI",       "2511TL005663ISI"),
        ("https://go.fresh.com.eg/vb5vm?SN=2511TL005663ISI",       "2511TL005663ISI"),
        ("2511TL005663ISI",                                          "2511TL005663ISI"),
        ("  ABC-123  ",                                              "ABC-123"),
        ("https://example.com/path?SN=XYZ999",                      "XYZ999"),
        ("https://example.com/path/MYCODE",                          "MYCODE"),
        ("",                                                          ""),
    ]

    all_pass = True
    for raw, expected in tests:
        result = normalize_barcode(raw)
        status = "✅" if result == expected else "❌"
        if result != expected:
            all_pass = False
        print(f"{status}  input:    {raw!r}")
        print(f"     expected: {expected!r}")
        print(f"     got:      {result!r}")
        print()

    print("=" * 40)
    print("All tests passed! ✅" if all_pass else "Some tests FAILED ❌")
